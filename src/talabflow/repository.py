"""Data access and the transactional-outbox write path.

The one rule that makes this correct: **a status change and its customer notification are
written in the same transaction.** If they were separate, a crash between them would either
lose the notification or send one for a change that never committed. Here the notification is a
row, and the worker delivers it later from durable state.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .messages import Language, render, status_label
from .models import (
    ALLOWED_TRANSITIONS,
    ConversationState,
    Customer,
    Order,
    OrderEvent,
    OrderStatus,
    OutboxMessage,
    OutboxStatus,
    StaffRole,
    StaffUser,
    utcnow,
)
from .references import generate_reference
from .security import hash_password

logger = logging.getLogger(__name__)

#: How many times to retry on a reference collision before giving up.
_REFERENCE_ATTEMPTS = 8


class OrderNotFoundError(LookupError):
    """No order matches the supplied reference."""


class InvalidTransitionError(ValueError):
    """The requested status change is not allowed from the order's current status."""


class DuplicateUserError(ValueError):
    """A staff user with that username already exists."""


@dataclass(frozen=True, slots=True)
class OrderPage:
    """One page of orders plus the unfiltered total, for pagination."""

    items: list[Order]
    total: int
    limit: int
    offset: int


# --------------------------------------------------------------------------- customers


def get_or_create_customer(
    session: Session, *, channel: str, channel_user_id: str, chat_id: str, display_name: str | None
) -> Customer:
    """Find the customer for this channel identity, creating them on first contact."""
    customer = session.scalars(
        select(Customer).where(
            Customer.channel == channel, Customer.channel_user_id == channel_user_id
        )
    ).one_or_none()
    if customer is not None:
        # A customer can change their display name, and chat_id can differ from user_id.
        if display_name and customer.display_name != display_name:
            customer.display_name = display_name
        if customer.chat_id != chat_id:
            customer.chat_id = chat_id
        return customer

    customer = Customer(
        channel=channel,
        channel_user_id=channel_user_id,
        chat_id=chat_id,
        display_name=display_name,
    )
    session.add(customer)
    session.flush()
    return customer


def get_conversation_state(session: Session, customer: Customer, *, default_step: str) -> ConversationState:
    """Load the customer's conversation state, creating it at ``default_step`` if absent."""
    state = session.scalars(
        select(ConversationState).where(ConversationState.customer_id == customer.id)
    ).one_or_none()
    if state is None:
        state = ConversationState(customer_id=customer.id, step=default_step)
        session.add(state)
        session.flush()
    return state


# --------------------------------------------------------------------------- orders


def create_order(
    session: Session,
    *,
    customer: Customer,
    service_type: str,
    details: str,
    contact_phone: str,
    address: str,
    actor: str = "customer",
) -> Order:
    """Create an order with its creation event, retrying on a reference collision.

    The retry loop uses a SAVEPOINT so a collision does not poison the outer transaction --
    without it, the ``IntegrityError`` would abort everything the caller had done so far.
    """
    for attempt in range(_REFERENCE_ATTEMPTS):
        reference = generate_reference()
        try:
            with session.begin_nested():
                order = Order(
                    reference=reference,
                    customer_id=customer.id,
                    service_type=service_type,
                    details=details,
                    contact_phone=contact_phone,
                    address=address,
                    status=OrderStatus.NEW,
                )
                session.add(order)
                session.flush()
                session.add(
                    OrderEvent(
                        order_id=order.id,
                        from_status=None,
                        to_status=OrderStatus.NEW,
                        actor=actor,
                        note="order created",
                    )
                )
                session.flush()
            return order
        except IntegrityError:
            logger.warning(
                "reference collision on %s (attempt %d); regenerating", reference, attempt + 1
            )
    raise RuntimeError(
        f"could not allocate a unique order reference after {_REFERENCE_ATTEMPTS} attempts"
    )


def get_order_by_reference(session: Session, reference: str) -> Order | None:
    return session.scalars(
        select(Order).where(Order.reference == reference).options(selectinload(Order.customer))
    ).one_or_none()


def get_customer_order(session: Session, *, customer_id: int, reference: str) -> Order | None:
    """Fetch an order **scoped to one customer**.

    The scoping is the security control behind the ``/status`` command: without it, anyone who
    guessed or overheard a reference could read another customer's phone number and address.
    """
    return session.scalars(
        select(Order).where(Order.reference == reference, Order.customer_id == customer_id)
    ).one_or_none()


def list_orders(
    session: Session,
    *,
    status: OrderStatus | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> OrderPage:
    """List orders newest-first, optionally filtered by status and a free-text search."""
    filters = []
    if status is not None:
        filters.append(Order.status == status)
    if search:
        # A bound LIKE pattern: the wildcards are ours, the value stays a parameter.
        pattern = f"%{search.strip()}%"
        filters.append(
            or_(
                Order.reference.ilike(pattern),
                Order.service_type.ilike(pattern),
                Order.details.ilike(pattern),
                Order.contact_phone.ilike(pattern),
                Order.address.ilike(pattern),
            )
        )

    total = session.scalar(select(func.count()).select_from(Order).where(*filters)) or 0
    items = list(
        session.scalars(
            select(Order)
            .where(*filters)
            .options(selectinload(Order.customer))
            .order_by(Order.created_at.desc(), Order.id.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    return OrderPage(items=items, total=total, limit=limit, offset=offset)


def load_order_events(session: Session, order_id: int) -> list[OrderEvent]:
    return list(
        session.scalars(
            select(OrderEvent).where(OrderEvent.order_id == order_id).order_by(OrderEvent.id)
        )
    )


def change_order_status(
    session: Session,
    *,
    order: Order,
    to_status: OrderStatus,
    actor: str,
    note: str | None = None,
    notify: bool = True,
    language: Language = "en",
) -> OrderEvent:
    """Move ``order`` to ``to_status``, writing the audit event and the notification atomically.

    Raises:
        InvalidTransitionError: if the transition is not in :data:`ALLOWED_TRANSITIONS`. The
            message names the permitted targets so the caller need not guess.
    """
    current = order.status
    allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
    if to_status not in allowed:
        permitted = ", ".join(sorted(item.value for item in allowed)) or "nothing (terminal)"
        raise InvalidTransitionError(
            f"cannot move order {order.reference} from {current.value} to {to_status.value}; "
            f"allowed from {current.value}: {permitted}"
        )

    order.status = to_status
    order.updated_at = utcnow()
    event = OrderEvent(
        order_id=order.id, from_status=current, to_status=to_status, actor=actor, note=note
    )
    session.add(event)
    # Flush so the event has its id -- the idempotency key is derived from it, which is what
    # makes one notification per *event* rather than per status value.
    session.flush()

    if notify:
        queue_status_notification(session, order=order, event=event, language=language)
    return event


# --------------------------------------------------------------------------- outbox


def outbox_key_for_event(event_id: int) -> str:
    """The idempotency key for an event's notification.

    Derived from the event id, so replaying the same logical change produces the same key and
    the unique constraint collapses it to one delivery.
    """
    return f"order-event:{event_id}"


def queue_status_notification(
    session: Session, *, order: Order, event: OrderEvent, language: Language = "en"
) -> OutboxMessage | None:
    """Queue the customer notification for ``event``.

    Returns ``None`` when a message for this event already exists -- that is the idempotent
    path, not an error.
    """
    key = outbox_key_for_event(event.id)
    existing = session.scalars(
        select(OutboxMessage).where(OutboxMessage.idempotency_key == key)
    ).one_or_none()
    if existing is not None:
        logger.debug("notification for event %d already queued", event.id)
        return None

    customer = order.customer or session.get(Customer, order.customer_id)
    if customer is None:  # pragma: no cover - guarded by a NOT NULL foreign key
        raise RuntimeError(f"order {order.reference} has no customer")

    note = f"\n{event.note}" if event.note and event.note != "order created" else ""
    body = render(
        "status_changed",
        language,
        reference=order.reference,
        status=status_label(order.status.value, language),
        note=note,
    )
    message = OutboxMessage(
        idempotency_key=key,
        order_id=order.id,
        channel=customer.channel,
        chat_id=customer.chat_id,
        body=body,
    )
    session.add(message)
    try:
        session.flush()
    except IntegrityError:
        # Another worker or request queued it between the check and the insert. The unique
        # constraint is the real guarantee; this branch just makes the race harmless.
        session.rollback()
        logger.info("notification for event %d was queued concurrently", event.id)
        return None
    return message


def claim_due_outbox_messages(
    session: Session, *, limit: int, now: datetime | None = None
) -> list[OutboxMessage]:
    """Return pending/failed messages whose retry time has arrived, oldest first."""
    moment = now or utcnow()
    return list(
        session.scalars(
            select(OutboxMessage)
            .where(
                OutboxMessage.status.in_([OutboxStatus.PENDING, OutboxStatus.FAILED]),
                OutboxMessage.next_attempt_at <= moment,
            )
            .order_by(OutboxMessage.next_attempt_at, OutboxMessage.id)
            .limit(limit)
        )
    )


# --------------------------------------------------------------------------- staff


def create_staff_user(
    session: Session, *, username: str, password: str, role: StaffRole = StaffRole.STAFF
) -> StaffUser:
    """Create a staff user with a hashed password.

    Raises:
        DuplicateUserError: if the username is taken.
        PasswordPolicyError: if the password is too short.
    """
    username = username.strip().lower()
    if not username:
        raise ValueError("username must not be empty")
    existing = session.scalars(select(StaffUser).where(StaffUser.username == username)).one_or_none()
    if existing is not None:
        raise DuplicateUserError(f"a user named {username!r} already exists")

    user = StaffUser(username=username, password_hash=hash_password(password), role=role)
    session.add(user)
    session.flush()
    return user


def get_staff_user(session: Session, username: str) -> StaffUser | None:
    return session.scalars(
        select(StaffUser).where(StaffUser.username == username.strip().lower())
    ).one_or_none()


def list_staff_users(session: Session) -> Sequence[StaffUser]:
    return list(session.scalars(select(StaffUser).order_by(StaffUser.id)))


def count_orders_by_status(session: Session) -> dict[str, int]:
    """Order counts per status, including statuses with none, so a dashboard has no gaps."""
    rows = session.execute(select(Order.status, func.count()).group_by(Order.status)).all()
    counts = {status.value: 0 for status in OrderStatus}
    for status, count in rows:
        counts[status.value] = int(count)
    return counts
