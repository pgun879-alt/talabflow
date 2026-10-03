"""Data access and the transactional-outbox write path.

The one rule that makes this correct: **a status change and its customer notification are
written in the same transaction.** If they were separate, a crash between them would either
lose the notification or send one for a change that never committed. Here the notification is a
row, and the worker delivers it later from durable state.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.orm.attributes import set_committed_value
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

#: Characters people put inside a phone number, and what is left once they are removed.
_PHONE_FORMATTING = re.compile(r"[\s().\-]")
_PHONE_SEARCH = re.compile(r"\+?\d{3,}")

#: How many times to retry on a reference collision before giving up.
_REFERENCE_ATTEMPTS = 8

#: Width of ``customers.display_name``. A transport can hand over a longer name than this --
#: Telegram allows 64 characters each for a first and last name, 129 with the space -- and
#: PostgreSQL enforces the column length where SQLite silently does not.
_MAX_DISPLAY_NAME = 128


class OrderNotFoundError(LookupError):
    """No order matches the supplied reference."""


class InvalidTransitionError(ValueError):
    """The requested status change is not allowed from the order's current status."""


class ConcurrentStatusChangeError(InvalidTransitionError):
    """The order's status changed between this caller reading it and trying to change it.

    A subclass of :class:`InvalidTransitionError` on purpose: to the caller it is the same
    situation -- the transition they asked for is not valid from where the order *now* is -- and
    the API already maps that to ``409 Conflict``.
    """


class DuplicateUserError(ValueError):
    """A staff user with that username already exists."""


class StaffNotFoundError(LookupError):
    """No staff user has that username."""


class LastAdminError(ValueError):
    """The change would leave the system with no active admin.

    Nobody could then create, reactivate or promote an account through the product at all, so the
    change is refused rather than left to be repaired by hand in the database.
    """


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
    if display_name:
        # Truncate rather than fail: a name is cosmetic, and an insert that fails on it would
        # leave the customer with no reply at all.
        display_name = display_name[:_MAX_DISPLAY_NAME]
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
    contact_phone_verified: bool = False,
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
                    contact_phone_verified=contact_phone_verified,
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
        # A bound LIKE pattern: the wildcards are ours, the value stays a parameter. Binding
        # alone stops SQL injection but not LIKE's own wildcards -- an unescaped "%" in the search
        # term would match every order -- so those are escaped as well.
        term = search.strip()
        for special in ("\\", "%", "_"):
            term = term.replace(special, "\\" + special)
        pattern = f"%{term}%"
        matches = [
            Order.reference.ilike(pattern, escape="\\"),
            Order.service_type.ilike(pattern, escape="\\"),
            Order.details.ilike(pattern, escape="\\"),
            Order.contact_phone.ilike(pattern, escape="\\"),
            Order.address.ilike(pattern, escape="\\"),
        ]
        # Phone numbers are stored in E.164 (+213555123456), but staff search the way they dial:
        # "0555 12 34 56". Strip the formatting and the leading trunk zeros or "+" so the digits
        # that the two forms share are what gets matched.
        compact = _PHONE_FORMATTING.sub("", search)
        if _PHONE_SEARCH.fullmatch(compact):
            national = compact.lstrip("+").lstrip("0")
            if national:
                matches.append(Order.contact_phone.ilike(f"%{national}%"))
        filters.append(or_(*matches))

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
        ConcurrentStatusChangeError: if someone else changed the order's status after ``order``
            was loaded. Nothing is written in that case.
    """
    current = order.status
    allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
    if to_status not in allowed:
        permitted = ", ".join(sorted(item.value for item in allowed)) or "nothing (terminal)"
        raise InvalidTransitionError(
            f"cannot move order {order.reference} from {current.value} to {to_status.value}; "
            f"allowed from {current.value}: {permitted}"
        )

    # Compare-and-swap, not a plain assignment. The check above ran against the copy of the order
    # this caller loaded, which may be stale: two members of staff can load the same ``ready``
    # order, and if one cancels it the other's "completed" is still valid *from ready*. Writing
    # it unconditionally would bring a cancelled order back to life, record two changes out of
    # the same status, and notify the customer of both.
    #
    # Putting the expected status in the WHERE clause makes the database the arbiter. SQLite
    # serialises writers; PostgreSQL blocks the second UPDATE on the row lock and then re-checks
    # the predicate against the committed row. Either way exactly one of them matches.
    moment = utcnow()
    swapped = session.execute(
        update(Order)
        .where(Order.id == order.id, Order.status == current)
        .values(status=to_status, updated_at=moment)
        .execution_options(synchronize_session=False)
    )
    if int(cast("CursorResult[Any]", swapped).rowcount or 0) != 1:
        session.refresh(order)
        raise ConcurrentStatusChangeError(
            f"order {order.reference} was changed by someone else while this request was in "
            f"progress: it is now {order.status.value}, not {current.value}. Reload it and try "
            "again"
        )
    # The row is already updated; bring the loaded object in line without marking it dirty, so
    # the unit of work does not issue a second, unconditional UPDATE at flush.
    set_committed_value(order, "status", to_status)
    set_committed_value(order, "updated_at", moment)
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


def find_outbox_by_key(session: Session, key: str) -> OutboxMessage | None:
    """Look up a queued notification by idempotency key.

    Split out of :func:`queue_status_notification` on purpose. This check is only a fast path --
    the unique constraint is the actual guarantee -- and between this read and the insert another
    process can take the key. That window is precisely what the savepoint below protects against,
    and making the lookup a named function lets a test stub it to reproduce the race
    deterministically instead of hoping to hit it by timing.
    """
    return session.scalars(
        select(OutboxMessage).where(OutboxMessage.idempotency_key == key)
    ).one_or_none()


def queue_status_notification(
    session: Session, *, order: Order, event: OrderEvent, language: Language = "en"
) -> OutboxMessage | None:
    """Queue the customer notification for ``event``.

    Returns ``None`` when a message for this event already exists -- that is the idempotent
    path, not an error.
    """
    key = outbox_key_for_event(event.id)
    if find_outbox_by_key(session, key) is not None:
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
    # The insert runs inside a SAVEPOINT so that losing the unique-key race rolls back *only*
    # this insert.
    #
    # The previous version called session.rollback() here, which rolls back the entire outer
    # transaction -- and that transaction also contains the order's new status and its OrderEvent
    # audit row. A concurrent duplicate enqueue would therefore have silently discarded a status
    # change that the caller had already been told succeeded. A savepoint keeps the caller's work
    # and leaves the outer transaction usable.
    try:
        with session.begin_nested():
            session.add(message)
            session.flush()
    except IntegrityError:
        # Another worker or request inserted the same idempotency_key between the check above and
        # this insert. The unique constraint is the real guarantee; this branch only makes losing
        # the race harmless.
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


def renew_claim(
    session: Session,
    message: OutboxMessage,
    *,
    worker_id: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> bool:
    """Extend ``worker_id``'s lease on ``message`` and commit it, or report the lease as lost.

    A batch is claimed at one moment but sent one message at a time, so by the time the worker
    reaches the later messages their leases may have run out and another worker may already have
    reclaimed -- or sent -- them. The worker therefore calls this immediately before each send.

    The update is conditional on the row still being ``processing`` under *this* claim
    (``claimed_by`` and ``claimed_at`` both unchanged), so it doubles as the ownership check:

    * ``True`` -- the row is still ours and now carries a fresh, full-length lease, committed
      before the outbound call exactly as the original claim was.
    * ``False`` -- someone else holds or has finished the row. The caller must not send it and
      must not write to it.
    """
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    moment = now or utcnow()
    expires_at = moment + timedelta(seconds=lease_seconds)
    result = session.execute(
        update(OutboxMessage)
        .where(
            OutboxMessage.id == message.id,
            OutboxMessage.status == OutboxStatus.PROCESSING,
            OutboxMessage.claimed_by == worker_id,
            OutboxMessage.claimed_at == message.claimed_at,
        )
        .values(lease_expires_at=expires_at)
        .execution_options(synchronize_session=False)
    )
    session.commit()
    if int(cast("CursorResult[Any]", result).rowcount or 0) != 1:
        return False
    set_committed_value(message, "lease_expires_at", expires_at)
    return True


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


def update_staff_user(
    session: Session,
    username: str,
    *,
    is_active: bool | None = None,
    role: StaffRole | None = None,
    password: str | None = None,
) -> StaffUser:
    """Deactivate or reactivate a staff user, change their role, or set a new password.

    Each argument left as ``None`` is left unchanged. Deactivation and demotion take effect on
    the user's *next request*, because the API re-reads the account on every call instead of
    trusting the token. A new password does not cancel tokens already issued; they run out on
    their own, within the token lifetime. Deactivate the account to cut access at once.

    Raises:
        StaffNotFoundError: if there is no such user.
        LastAdminError: if the change would deactivate or demote the only active admin.
        PasswordPolicyError: if the new password is too short.
    """
    user = get_staff_user(session, username)
    if user is None:
        raise StaffNotFoundError(f"no staff user named {username.strip().lower()!r}")

    new_active = user.is_active if is_active is None else is_active
    new_role = user.role if role is None else role
    is_admin_now = user.is_active and user.role is StaffRole.ADMIN
    stays_admin = new_active and new_role is StaffRole.ADMIN
    if is_admin_now and not stays_admin:
        other_admins = session.scalar(
            select(func.count())
            .select_from(StaffUser)
            .where(
                StaffUser.role == StaffRole.ADMIN,
                StaffUser.is_active.is_(True),
                StaffUser.id != user.id,
            )
        )
        if not other_admins:
            raise LastAdminError(
                f"{user.username!r} is the only active admin; promote or reactivate another "
                "admin first"
            )

    if password is not None:
        user.password_hash = hash_password(password)
    user.is_active = new_active
    user.role = new_role
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
