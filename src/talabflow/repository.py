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
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.sql.elements import ColumnElement

from .messages import Language, render, status_label
from .models import (
    ALLOWED_TRANSITIONS,
    CLAIMABLE_OUTBOX_STATUSES,
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


def get_conversation_state(
    session: Session, customer: Customer, *, default_step: str
) -> ConversationState:
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


def _claimable(moment: datetime) -> ColumnElement[bool]:
    """Predicate for "this row may be claimed right now".

    Either it is waiting and its retry time has arrived, or it is ``processing`` under a lease
    that has expired -- which is how a message survives the worker that claimed it crashing.
    """
    return or_(
        and_(
            OutboxMessage.status.in_(tuple(CLAIMABLE_OUTBOX_STATUSES)),
            OutboxMessage.next_attempt_at <= moment,
        ),
        and_(
            OutboxMessage.status == OutboxStatus.PROCESSING,
            OutboxMessage.lease_expires_at.is_not(None),
            OutboxMessage.lease_expires_at <= moment,
        ),
    )


def due_outbox_messages(
    session: Session, *, limit: int = 100, now: datetime | None = None
) -> list[OutboxMessage]:
    """Read-only view of what *could* be claimed. Does not claim anything.

    For inspection, tests and the CLI. Workers must use :func:`claim_outbox_batch`.
    """
    moment = now or utcnow()
    return list(
        session.scalars(
            select(OutboxMessage)
            .where(_claimable(moment))
            .order_by(OutboxMessage.next_attempt_at, OutboxMessage.id)
            .limit(limit)
        )
    )


def claim_outbox_batch(
    session: Session,
    *,
    worker_id: str,
    limit: int,
    lease_seconds: int,
    now: datetime | None = None,
) -> list[OutboxMessage]:
    """Atomically lease up to ``limit`` due messages to ``worker_id``, and commit the lease.

    Why a single guarded ``UPDATE``
    -------------------------------
    Selecting due rows and then sending them is not safe with more than one worker: both would
    select the same rows and both would send. The claim therefore has to be atomic.

    This issues **one** statement::

        UPDATE outbox_messages
           SET status='processing', claimed_by=..., lease_expires_at=...
         WHERE id IN (SELECT id ... WHERE <claimable> ORDER BY ... LIMIT n)
           AND <claimable>          -- repeated deliberately

    The repeated predicate in the outer ``WHERE`` is the part that matters, and it is not
    redundant:

    * On **PostgreSQL**, two concurrent statements can both pick the same id in their subqueries.
      The second blocks on the row lock, and when it proceeds PostgreSQL re-evaluates the outer
      ``WHERE`` against the newly committed row (``EvalPlanQual``). Without the repeated
      predicate the row still matches by id and the second worker would overwrite the first
      worker's lease. With it, the row is now ``processing`` with a future lease, the predicate
      fails, and the row is not claimed.
    * On **SQLite**, writes are serialised and a single ``UPDATE`` is atomic, so the second
      worker's subquery simply sees the already-claimed rows and skips them.

    The lease is **committed before returning**, so it is durable before any outbound call is
    made. That ordering is the whole point: a crash after sending but before recording the send
    leaves a claimed row whose lease expires, not an unclaimed row that a second worker resends
    immediately.

    Returns:
        The rows this worker now owns, oldest first. Empty when there is nothing to do.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")

    moment = now or utcnow()
    candidate_ids = (
        select(OutboxMessage.id)
        .where(_claimable(moment))
        .order_by(OutboxMessage.next_attempt_at, OutboxMessage.id)
        .limit(limit)
        .scalar_subquery()
    )
    session.execute(
        update(OutboxMessage)
        .where(OutboxMessage.id.in_(candidate_ids), _claimable(moment))
        .values(
            status=OutboxStatus.PROCESSING,
            claimed_by=worker_id,
            claimed_at=moment,
            lease_expires_at=moment + timedelta(seconds=lease_seconds),
        )
        .execution_options(synchronize_session=False)
    )
    # Commit so the lease is visible to every other worker before we send anything.
    session.commit()

    claimed = list(
        session.scalars(
            select(OutboxMessage)
            .where(
                OutboxMessage.status == OutboxStatus.PROCESSING,
                OutboxMessage.claimed_by == worker_id,
                OutboxMessage.claimed_at == moment,
            )
            .order_by(OutboxMessage.next_attempt_at, OutboxMessage.id)
        )
    )
    if claimed:
        logger.info(
            "claimed outbox messages",
            extra={"worker": worker_id, "claimed": len(claimed)},
        )
    return claimed


def release_claim(message: OutboxMessage) -> None:
    """Clear the lease fields once a row has reached a terminal or waiting state.

    Leaving a stale ``claimed_by`` behind would make it impossible to tell a live lease from a
    finished one when reading the table by hand.
    """
    message.claimed_by = None
    message.claimed_at = None
    message.lease_expires_at = None


def reclaim_expired_leases(session: Session, *, now: datetime | None = None) -> int:
    """Return ``processing`` rows with an expired lease to ``failed`` so they are retried.

    The claim query already treats an expired lease as claimable, so this is an explicit
    housekeeping path for an operator, not something the worker depends on.
    """
    moment = now or utcnow()
    result = session.execute(
        update(OutboxMessage)
        .where(
            OutboxMessage.status == OutboxStatus.PROCESSING,
            OutboxMessage.lease_expires_at.is_not(None),
            OutboxMessage.lease_expires_at <= moment,
        )
        .values(
            status=OutboxStatus.FAILED,
            claimed_by=None,
            claimed_at=None,
            lease_expires_at=None,
            last_error="lease expired; the worker holding this message did not finish",
        )
        .execution_options(synchronize_session=False)
    )
    # Session.execute is typed as returning Result, but an UPDATE always yields a CursorResult,
    # which is where rowcount lives.
    return int(cast("CursorResult[Any]", result).rowcount or 0)


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
    existing = session.scalars(
        select(StaffUser).where(StaffUser.username == username)
    ).one_or_none()
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
