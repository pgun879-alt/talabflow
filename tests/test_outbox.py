"""Tests for the notification worker: delivery, retry, backoff and dead-lettering."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from talabflow import repository
from talabflow.db import session_scope
from talabflow.models import OrderStatus, OutboxMessage, OutboxStatus, utcnow
from talabflow.outbox import OutboxWorker, backoff_delay
from talabflow.transports.scripted import ScriptedTransport


@pytest.fixture
def queued_order(session_factory: sessionmaker[Session]) -> str:
    """An order with one pending notification. Returns the reference."""
    with session_scope(session_factory) as session:
        customer = repository.get_or_create_customer(
            session, channel="scripted", channel_user_id="1001", chat_id="1001", display_name="A"
        )
        order = repository.create_order(
            session,
            customer=customer,
            service_type="Repair",
            details="The washing machine will not drain",
            contact_phone="0555123456",
            address="12 Rue Didouche Mourad",
        )
        repository.change_order_status(
            session, order=order, to_status=OrderStatus.CONFIRMED, actor="amina"
        )
        return order.reference


def _messages(session_factory: sessionmaker[Session]) -> list[OutboxMessage]:
    with session_scope(session_factory) as session:
        return list(session.scalars(select(OutboxMessage).order_by(OutboxMessage.id)))


# --------------------------------------------------------------------- backoff


def test_backoff_is_exponential_and_capped() -> None:
    assert backoff_delay(1, base_seconds=30) == timedelta(seconds=30)
    assert backoff_delay(2, base_seconds=30) == timedelta(seconds=60)
    assert backoff_delay(3, base_seconds=30) == timedelta(seconds=120)
    assert backoff_delay(20, base_seconds=30, cap_seconds=3600) == timedelta(seconds=3600)


def test_backoff_treats_a_nonsense_attempt_count_as_the_first() -> None:
    assert backoff_delay(0, base_seconds=30) == timedelta(seconds=30)
    assert backoff_delay(-5, base_seconds=30) == timedelta(seconds=30)


# --------------------------------------------------------------------- delivery


def test_a_pending_message_is_delivered_and_marked_sent(
    worker: OutboxWorker,
    transport: ScriptedTransport,
    queued_order: str,
    session_factory: sessionmaker[Session],
) -> None:
    sent, failed = worker.process_batch()
    assert (sent, failed) == (1, 0)
    assert queued_order in transport.last_text()

    (message,) = _messages(session_factory)
    assert message.status is OutboxStatus.SENT
    assert message.attempts == 1
    assert message.sent_at is not None
    assert message.last_error is None


def test_a_sent_message_is_never_delivered_again(
    worker: OutboxWorker,
    transport: ScriptedTransport,
    queued_order: str,
) -> None:
    """The guarantee the buyer cares about: the customer is not messaged twice."""
    worker.process_batch()
    assert len(transport.sent) == 1
    for _ in range(3):
        assert worker.process_batch() == (0, 0)
    assert len(transport.sent) == 1


def test_an_empty_outbox_is_not_an_error(worker: OutboxWorker) -> None:
    assert worker.process_batch() == (0, 0)


# --------------------------------------------------------------------- retry


def test_a_transient_failure_is_retried_with_backoff(
    worker: OutboxWorker,
    transport: ScriptedTransport,
    queued_order: str,
    session_factory: sessionmaker[Session],
) -> None:
    transport.fail_next_sends = 1
    before = utcnow()
    sent, failed = worker.process_batch()
    assert (sent, failed) == (0, 1)

    (message,) = _messages(session_factory)
    assert message.status is OutboxStatus.FAILED
    assert message.attempts == 1
    assert message.last_error is not None
    assert message.next_attempt_at > before, "a retry must be scheduled into the future"


def test_a_message_not_yet_due_is_skipped(
    worker: OutboxWorker,
    transport: ScriptedTransport,
    queued_order: str,
    session_factory: sessionmaker[Session],
) -> None:
    transport.fail_next_sends = 1
    worker.process_batch()
    # The backoff pushed next_attempt_at forward, so an immediate second pass finds nothing.
    assert worker.process_batch() == (0, 0)


def test_a_retry_succeeds_once_the_transport_recovers(
    worker: OutboxWorker,
    transport: ScriptedTransport,
    queued_order: str,
    session_factory: sessionmaker[Session],
) -> None:
    transport.fail_next_sends = 1
    worker.process_batch()

    # Simulate the backoff having elapsed rather than sleeping through it.
    with session_scope(session_factory) as session:
        message = session.scalars(select(OutboxMessage)).one()
        message.next_attempt_at = utcnow() - timedelta(seconds=1)

    assert worker.process_batch() == (1, 0)
    (message,) = _messages(session_factory)
    assert message.status is OutboxStatus.SENT
    assert message.attempts == 2
    assert message.last_error is None, "a successful retry must clear the stale error"


def test_repeated_failures_exhaust_the_attempt_budget_and_dead_letter(
    worker: OutboxWorker,
    transport: ScriptedTransport,
    queued_order: str,
    session_factory: sessionmaker[Session],
) -> None:
    transport.fail_next_sends = 99  # always fail
    for _ in range(worker.settings.outbox_max_attempts):
        with session_scope(session_factory) as session:
            for message in session.scalars(select(OutboxMessage)):
                message.next_attempt_at = utcnow() - timedelta(seconds=1)
        worker.process_batch()

    (message,) = _messages(session_factory)
    assert message.status is OutboxStatus.DEAD
    assert message.attempts == worker.settings.outbox_max_attempts
    # A dead message is not retried forever.
    assert worker.process_batch() == (0, 0)


def test_a_permanent_failure_skips_the_retry_budget_entirely(
    worker: OutboxWorker,
    transport: ScriptedTransport,
    queued_order: str,
    session_factory: sessionmaker[Session],
) -> None:
    """A customer who blocked the bot will never receive the message; retrying wastes attempts
    and, against a real provider, risks rate limits."""
    transport.permanent_failure = True
    sent, failed = worker.process_batch()
    assert (sent, failed) == (0, 1)

    (message,) = _messages(session_factory)
    assert message.status is OutboxStatus.DEAD
    assert message.attempts == 1, "one attempt, not the full budget"
    assert "permanent" in (message.last_error or "")


# --------------------------------------------------------------------- batching


def test_the_batch_size_is_respected(
    settings,
    transport: ScriptedTransport,
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory) as session:
        customer = repository.get_or_create_customer(
            session, channel="scripted", channel_user_id="1", chat_id="1", display_name="A"
        )
        for _ in range(5):
            order = repository.create_order(
                session,
                customer=customer,
                service_type="Repair",
                details="some details here",
                contact_phone="0555123456",
                address="an address",
            )
            repository.change_order_status(
                session, order=order, to_status=OrderStatus.CONFIRMED, actor="amina"
            )

    small = OutboxWorker(
        settings=settings.model_copy(update={"outbox_batch_size": 2}),
        transport=transport,
        session_factory=session_factory,
    )
    assert small.process_batch() == (2, 0)
    assert small.process_batch() == (2, 0)
    assert small.process_batch() == (1, 0)
    assert small.process_batch() == (0, 0)


def test_run_forever_stops_at_max_iterations(
    worker: OutboxWorker, transport: ScriptedTransport, queued_order: str
) -> None:
    sent, failed = worker.run_forever(max_iterations=1)
    assert (sent, failed) == (1, 0)


def test_request_stop_ends_the_loop(worker: OutboxWorker) -> None:
    worker.request_stop()
    assert worker.run_forever() == (0, 0)


def test_one_failing_message_does_not_block_the_others(
    worker: OutboxWorker,
    transport: ScriptedTransport,
    session_factory: sessionmaker[Session],
) -> None:
    """Head-of-line blocking would mean one blocked customer stops every other notification."""
    with session_scope(session_factory) as session:
        customer = repository.get_or_create_customer(
            session, channel="scripted", channel_user_id="1", chat_id="1", display_name="A"
        )
        for _ in range(3):
            order = repository.create_order(
                session,
                customer=customer,
                service_type="Repair",
                details="some details here",
                contact_phone="0555123456",
                address="an address",
            )
            repository.change_order_status(
                session, order=order, to_status=OrderStatus.CONFIRMED, actor="amina"
            )

    transport.fail_next_sends = 1  # only the first send fails
    sent, failed = worker.process_batch()
    assert failed == 1
    assert sent == 2, "the remaining messages must still go out in the same batch"
