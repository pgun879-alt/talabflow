"""Tests for model-level invariants: the status table and timestamp handling."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import StatementError
from sqlalchemy.orm import Session, sessionmaker

from talabflow import repository
from talabflow.models import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    Customer,
    OrderStatus,
    StaffUser,
    utcnow,
)


@pytest.fixture
def customer(session: Session) -> Customer:
    return repository.get_or_create_customer(
        session, channel="scripted", channel_user_id="1001", chat_id="1001", display_name="A"
    )


# --------------------------------------------------------------- timezone handling


def test_timestamps_come_back_timezone_aware(session: Session, customer: Customer) -> None:
    """Regression guard for a bug that only appeared once real date maths was attempted.

    SQLite hands back a naive datetime even for a ``DateTime(timezone=True)`` column. Writes and
    SQL-side comparisons still worked, so nothing failed until the retry scheduler compared
    ``next_attempt_at`` with an aware ``utcnow()`` and raised
    ``TypeError: can't compare offset-naive and offset-aware datetimes`` -- on the error path,
    which is the worst place to discover it. ``UtcDateTime`` normalises on load.
    """
    order = repository.create_order(
        session,
        customer=customer,
        service_type="Repair",
        details="something is broken",
        contact_phone="0555123456",
        address="an address",
    )
    session.commit()
    session.expire_all()  # force a real round trip through the database

    reloaded = repository.get_order_by_reference(session, order.reference)
    assert reloaded is not None
    for value in (reloaded.created_at, reloaded.updated_at):
        assert value.tzinfo is not None, "timestamps must be aware after a database round trip"
        assert value.utcoffset() == datetime.now(UTC).utcoffset()
    # The comparison that used to raise.
    assert reloaded.created_at <= utcnow()


def test_aware_timestamps_survive_a_round_trip_unchanged(
    session: Session, customer: Customer
) -> None:
    order = repository.create_order(
        session,
        customer=customer,
        service_type="Repair",
        details="something is broken",
        contact_phone="0555123456",
        address="an address",
    )
    event = repository.change_order_status(
        session, order=order, to_status=OrderStatus.CONFIRMED, actor="amina"
    )
    scheduled = utcnow()
    session.commit()
    session.expire_all()

    events = repository.load_order_events(session, order.id)
    assert all(item.created_at.tzinfo is not None for item in events)
    assert events[-1].id == event.id
    assert events[-1].created_at <= scheduled or events[-1].created_at.tzinfo is not None


def test_storing_a_naive_datetime_is_refused(session_factory: sessionmaker[Session]) -> None:
    """Fail loudly rather than silently guessing a timezone.

    SQLAlchemy wraps a bind-parameter error in ``StatementError``, so that is what a caller
    actually sees; the underlying ``ValueError`` message is preserved inside it.
    """
    opened = session_factory()
    try:
        opened.add(
            StaffUser(
                username="naive-timestamp",
                password_hash="x",
                created_at=datetime(2026, 1, 1, 12, 0),  # deliberately naive
            )
        )
        with pytest.raises(StatementError, match="naive datetime"):
            opened.flush()
    finally:
        opened.rollback()
        opened.close()


# --------------------------------------------------------------- status table


def test_every_status_has_a_transition_entry() -> None:
    """A status missing from the table would raise KeyError on any change attempt."""
    assert set(ALLOWED_TRANSITIONS) == set(OrderStatus)


def test_terminal_statuses_have_no_outgoing_transitions() -> None:
    for status in TERMINAL_STATUSES:
        assert ALLOWED_TRANSITIONS[status] == frozenset(), status


def test_non_terminal_statuses_all_have_somewhere_to_go() -> None:
    for status in OrderStatus:
        if status not in TERMINAL_STATUSES:
            assert ALLOWED_TRANSITIONS[status], f"{status} is a dead end but not terminal"


def test_no_transition_targets_an_unknown_status() -> None:
    for targets in ALLOWED_TRANSITIONS.values():
        assert targets <= set(OrderStatus)


def test_the_pipeline_is_acyclic_towards_completion() -> None:
    """Walking forward from NEW must reach COMPLETED without revisiting a status."""
    seen = [OrderStatus.NEW]
    current = OrderStatus.NEW
    while not current.is_terminal:
        forward = sorted(
            (
                target
                for target in ALLOWED_TRANSITIONS[current]
                if target is not OrderStatus.CANCELLED
            ),
            key=lambda item: item.value,
        )
        assert forward, f"no forward path from {current}"
        current = forward[0]
        assert current not in seen, f"cycle through {current}"
        seen.append(current)
    assert current is OrderStatus.COMPLETED
    assert len(seen) == 5


def test_is_terminal_reports_correctly() -> None:
    assert OrderStatus.COMPLETED.is_terminal
    assert OrderStatus.CANCELLED.is_terminal
    assert not OrderStatus.NEW.is_terminal
    assert not OrderStatus.READY.is_terminal
