"""SQLAlchemy 2.0 models.

Design decisions worth stating:

* **Statuses are a Python enum persisted as text.** Readable in the database, validated in the
  application, and the allowed *transitions* live in one table (:data:`ALLOWED_TRANSITIONS`)
  rather than being scattered across call sites.
* **Every status change writes an immutable event row.** The current status on ``Order`` is a
  cache of the latest event; ``OrderEvent`` is the audit trail. "Who moved this to cancelled
  and when" is the question a buyer actually asks.
* **Notifications are rows, not calls.** Writing an outbox row in the same transaction as the
  status change means a crash between "status changed" and "customer told" is impossible: the
  worker retries from durable state. The ``idempotency_key`` unique constraint is what makes
  redelivery safe.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import (
    Boolean,
    DateTime,
    Dialect,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Timezone-aware UTC now. Naive datetimes in a database are a bug waiting to happen."""
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator[datetime]):
    """A timestamp that is always timezone-aware UTC in Python, whatever the backend does.

    SQLite has no timezone-aware storage: it writes a datetime as a string and hands it back
    **naive**, even for a column declared ``DateTime(timezone=True)``. Every timestamp read out
    of the database would therefore be naive, and any comparison or subtraction against an aware
    value -- ``next_attempt_at > utcnow()``, for instance -- raises
    ``TypeError: can't compare offset-naive and offset-aware datetimes``.

    That failure is nastier than it first looks: writes still work and SQL-side comparisons
    still work, because everything is stored as UTC consistently. Only *Python-side* arithmetic
    breaks, so the bug hides until some code path does date maths -- which for a retry scheduler
    means it surfaces in production, on the error path, at the worst moment.

    Normalising in one type keeps the invariant in a single place instead of scattering
    ``.replace(tzinfo=UTC)`` across the codebase, and works identically on PostgreSQL, which
    does store the offset.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                "refusing to store a naive datetime; use talabflow.models.utcnow() so the "
                "intended timezone is never ambiguous"
            )
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            # SQLite: stored as UTC by construction, so re-attach the offset that was dropped.
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    """Declarative base for all models."""


class OrderStatus(str, enum.Enum):
    """The order pipeline."""

    NEW = "new"
    CONFIRMED = "confirmed"
    IN_PROGRESS = "in_progress"
    READY = "ready"
    COMPLETED = "completed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STATUSES


TERMINAL_STATUSES: Final[frozenset[OrderStatus]] = frozenset(
    {OrderStatus.COMPLETED, OrderStatus.CANCELLED}
)

#: The only status changes the system will perform. Anything else is rejected with an error
#: naming what *is* allowed, so a client never has to guess.
ALLOWED_TRANSITIONS: Final[dict[OrderStatus, frozenset[OrderStatus]]] = {
    OrderStatus.NEW: frozenset({OrderStatus.CONFIRMED, OrderStatus.CANCELLED}),
    OrderStatus.CONFIRMED: frozenset({OrderStatus.IN_PROGRESS, OrderStatus.CANCELLED}),
    OrderStatus.IN_PROGRESS: frozenset({OrderStatus.READY, OrderStatus.CANCELLED}),
    OrderStatus.READY: frozenset({OrderStatus.COMPLETED, OrderStatus.CANCELLED}),
    OrderStatus.COMPLETED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
}


class StaffRole(str, enum.Enum):
    """Who may do what. ``ADMIN`` is a superset of ``STAFF``."""

    ADMIN = "admin"
    STAFF = "staff"


class OutboxStatus(str, enum.Enum):
    """Lifecycle of a queued customer notification."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    DEAD = "dead"


class StaffUser(Base):
    """An operator of the admin API."""

    __tablename__ = "staff_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[StaffRole] = mapped_column(
        Enum(StaffRole, native_enum=False, length=16), nullable=False, default=StaffRole.STAFF
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        # Never include password_hash: a repr can end up in a log line or a traceback.
        return f"<StaffUser id={self.id} username={self.username!r} role={self.role.value}>"


class Customer(Base):
    """Someone who has messaged the bot.

    Identity is ``(channel, channel_user_id)`` -- a Telegram user id is only unique within
    Telegram, so the channel has to be part of the key for a second channel to be addable
    later without a migration that rewrites identities.
    """

    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint("channel", "channel_user_id", name="uq_customer_channel_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    channel_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(128))
    phone: Mapped[str | None] = mapped_column(String(32))
    is_blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now()
    )

    orders: Mapped[list[Order]] = relationship(back_populates="customer")

    def __repr__(self) -> str:
        return f"<Customer id={self.id} {self.channel}:{self.channel_user_id}>"


class Order(Base):
    """One customer request, tracked from intake to closure."""

    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reference: Mapped[str] = mapped_column(String(24), unique=True, nullable=False, index=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    service_type: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[str] = mapped_column(Text, nullable=False)
    contact_phone: Mapped[str] = mapped_column(String(32), nullable=False)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, native_enum=False, length=16),
        nullable=False,
        default=OrderStatus.NEW,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    customer: Mapped[Customer] = relationship(back_populates="orders")
    events: Mapped[list[OrderEvent]] = relationship(
        back_populates="order", order_by="OrderEvent.id", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Order {self.reference} status={self.status.value}>"


class OrderEvent(Base):
    """An immutable audit record of one status change.

    Rows are only ever inserted. ``from_status`` is ``None`` for the creation event.
    """

    __tablename__ = "order_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_status: Mapped[OrderStatus | None] = mapped_column(
        Enum(OrderStatus, native_enum=False, length=16)
    )
    to_status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, native_enum=False, length=16), nullable=False
    )
    actor: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="Staff username, or 'customer' / 'system'."
    )
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now()
    )

    order: Mapped[Order] = relationship(back_populates="events")

    def __repr__(self) -> str:
        return f"<OrderEvent order={self.order_id} -> {self.to_status.value} by {self.actor}>"


class OutboxMessage(Base):
    """A customer notification queued for delivery.

    Written in the *same transaction* as the status change that caused it, so the two cannot
    disagree. ``idempotency_key`` is unique, which is what makes the worker safe to run twice,
    to crash mid-send, or to be restarted without double-messaging a customer.
    """

    __tablename__ = "outbox_messages"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_outbox_idempotency_key"),
        Index("ix_outbox_status_next_attempt", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[OutboxStatus] = mapped_column(
        Enum(OutboxStatus, native_enum=False, length=16),
        nullable=False,
        default=OutboxStatus.PENDING,
        index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow
    )
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    def __repr__(self) -> str:
        return f"<OutboxMessage id={self.id} status={self.status.value} attempts={self.attempts}>"


class ConversationState(Base):
    """Where a customer is in the intake conversation.

    Persisted rather than held in memory so a restart does not lose a half-finished order --
    the single most annoying failure mode of a tutorial chat bot.
    """

    __tablename__ = "conversation_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), unique=True, nullable=False, index=True
    )
    step: Mapped[str] = mapped_column(String(32), nullable=False)
    draft_service_type: Mapped[str | None] = mapped_column(String(64))
    draft_details: Mapped[str | None] = mapped_column(Text)
    draft_phone: Mapped[str | None] = mapped_column(String(32))
    draft_address: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def reset_draft(self) -> None:
        """Clear the in-progress order fields, keeping the row for the next conversation."""
        self.draft_service_type = None
        self.draft_details = None
        self.draft_phone = None
        self.draft_address = None

    def __repr__(self) -> str:
        return f"<ConversationState customer={self.customer_id} step={self.step}>"
