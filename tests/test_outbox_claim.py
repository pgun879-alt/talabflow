"""Tests for outbox claiming, leases and crash recovery.

``claim_due_outbox_messages`` was a plain ``SELECT``, so two workers read the same due rows and
both sent them. A worker now leases a row and commits that lease before sending.

The concurrency tests use real threads against a real SQLite file, because the bug only appears
when two connections race.
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from talabflow import repository
from talabflow.config import Settings
from talabflow.db import build_session_factory, create_all, session_scope
from talabflow.models import (
    Order,
    OrderStatus,
    OutboxMessage,
    OutboxStatus,
    utcnow,
)
from talabflow.outbox import OutboxWorker
from talabflow.transports.base import OutboundMessage
from talabflow.transports.scripted import ScriptedTransport

WORKER_A = "worker-a"
WORKER_B = "worker-b"


def _seed_order(session: Session) -> Order:
    customer = repository.get_or_create_customer(
        session, channel="scripted", channel_user_id="1001", chat_id="1001", display_name="Amina"
    )
    return repository.create_order(
        session,
        customer=customer,
        service_type="Repair",
        details="The washing machine will not drain",
        contact_phone="0555123456",
        address="12 Rue Didouche Mourad",
    )


def _queue_notifications(factory: sessionmaker[Session], count: int) -> list[str]:
    """Create ``count`` orders, each advanced once so it has one pending notification."""
    keys = []
    with session_scope(factory) as session:
        customer = repository.get_or_create_customer(
            session, channel="scripted", channel_user_id="1", chat_id="1", display_name="A"
        )
        for _ in range(count):
            order = repository.create_order(
                session,
                customer=customer,
                service_type="Repair",
                details="something needs fixing here",
                contact_phone="0555123456",
                address="an address",
            )
            event_row = repository.change_order_status(
                session, order=order, to_status=OrderStatus.CONFIRMED, actor="amina"
            )
            keys.append(repository.outbox_key_for_event(event_row.id))
    return keys


def _all_messages(factory: sessionmaker[Session]) -> list[OutboxMessage]:
    with session_scope(factory) as session:
        return list(session.scalars(select(OutboxMessage).order_by(OutboxMessage.id)))


# ------------------------------------------------------------------ claiming basics


def test_claiming_marks_rows_processing_with_a_lease(
    session_factory: sessionmaker[Session],
) -> None:
    _queue_notifications(session_factory, 1)
    with session_scope(session_factory) as session:
        claimed = repository.claim_outbox_batch(
            session, worker_id=WORKER_A, limit=10, lease_seconds=60
        )
        assert len(claimed) == 1
        assert claimed[0].status is OutboxStatus.PROCESSING
        assert claimed[0].claimed_by == WORKER_A
        assert claimed[0].claimed_at is not None
        assert claimed[0].lease_expires_at is not None
        assert claimed[0].lease_expires_at > claimed[0].claimed_at


def test_a_claim_is_committed_before_it_is_returned(
    session_factory: sessionmaker[Session], engine: Engine
) -> None:
    """The claim must be durable before any send, so a *separate* connection must already see it."""
    _queue_notifications(session_factory, 1)
    with session_scope(session_factory) as session:
        repository.claim_outbox_batch(session, worker_id=WORKER_A, limit=10, lease_seconds=60)

    other = build_session_factory(engine)
    with session_scope(other) as fresh:
        row = fresh.scalars(select(OutboxMessage)).one()
        assert row.status is OutboxStatus.PROCESSING
        assert row.claimed_by == WORKER_A


def test_a_second_worker_cannot_claim_an_already_claimed_row(
    session_factory: sessionmaker[Session],
) -> None:
    """The core property. Without the claim this returned the same row to both workers."""
    _queue_notifications(session_factory, 1)
    with session_scope(session_factory) as first:
        assert (
            len(
                repository.claim_outbox_batch(first, worker_id=WORKER_A, limit=10, lease_seconds=60)
            )
            == 1
        )
    with session_scope(session_factory) as second:
        assert (
            repository.claim_outbox_batch(second, worker_id=WORKER_B, limit=10, lease_seconds=60)
            == []
        )


def test_two_workers_split_a_batch_rather_than_duplicating_it(
    session_factory: sessionmaker[Session],
) -> None:
    _queue_notifications(session_factory, 6)
    with session_scope(session_factory) as first:
        a = repository.claim_outbox_batch(first, worker_id=WORKER_A, limit=3, lease_seconds=60)
    with session_scope(session_factory) as second:
        b = repository.claim_outbox_batch(second, worker_id=WORKER_B, limit=3, lease_seconds=60)
    ids_a = {m.id for m in a}
    ids_b = {m.id for m in b}
    assert len(ids_a) == 3 and len(ids_b) == 3
    assert ids_a.isdisjoint(ids_b), "no message may be handed to both workers"


def test_a_message_not_yet_due_is_not_claimed(session_factory: sessionmaker[Session]) -> None:
    _queue_notifications(session_factory, 1)
    with session_scope(session_factory) as session:
        row = session.scalars(select(OutboxMessage)).one()
        row.next_attempt_at = utcnow() + timedelta(hours=1)
    with session_scope(session_factory) as session:
        assert (
            repository.claim_outbox_batch(session, worker_id=WORKER_A, limit=10, lease_seconds=60)
            == []
        )


def test_terminal_messages_are_never_claimed(session_factory: sessionmaker[Session]) -> None:
    _queue_notifications(session_factory, 2)
    with session_scope(session_factory) as session:
        rows = list(session.scalars(select(OutboxMessage).order_by(OutboxMessage.id)))
        rows[0].status = OutboxStatus.SENT
        rows[1].status = OutboxStatus.DEAD
    with session_scope(session_factory) as session:
        assert (
            repository.claim_outbox_batch(session, worker_id=WORKER_A, limit=10, lease_seconds=60)
            == []
        )


def test_invalid_claim_arguments_are_refused(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory) as session:
        with pytest.raises(ValueError, match="limit must be positive"):
            repository.claim_outbox_batch(session, worker_id=WORKER_A, limit=0, lease_seconds=60)
        with pytest.raises(ValueError, match="lease_seconds must be positive"):
            repository.claim_outbox_batch(session, worker_id=WORKER_A, limit=1, lease_seconds=0)


# ------------------------------------------------------------------ lease expiry


def test_an_expired_lease_is_reclaimable(session_factory: sessionmaker[Session]) -> None:
    """A worker that crashed between claiming and sending must not strand the message forever."""
    _queue_notifications(session_factory, 1)
    with session_scope(session_factory) as session:
        repository.claim_outbox_batch(session, worker_id=WORKER_A, limit=10, lease_seconds=60)

    # Simulate the lease having elapsed rather than sleeping through it.
    with session_scope(session_factory) as session:
        row = session.scalars(select(OutboxMessage)).one()
        row.lease_expires_at = utcnow() - timedelta(seconds=1)

    with session_scope(session_factory) as session:
        reclaimed = repository.claim_outbox_batch(
            session, worker_id=WORKER_B, limit=10, lease_seconds=60
        )
        assert len(reclaimed) == 1
        assert reclaimed[0].claimed_by == WORKER_B, "the new owner must take over the lease"


def test_a_live_lease_is_not_stolen(session_factory: sessionmaker[Session]) -> None:
    _queue_notifications(session_factory, 1)
    with session_scope(session_factory) as session:
        repository.claim_outbox_batch(session, worker_id=WORKER_A, limit=10, lease_seconds=3600)
    with session_scope(session_factory) as session:
        assert (
            repository.claim_outbox_batch(session, worker_id=WORKER_B, limit=10, lease_seconds=60)
            == []
        )


def test_reclaim_expired_leases_returns_rows_to_failed(
    session_factory: sessionmaker[Session],
) -> None:
    _queue_notifications(session_factory, 1)
    with session_scope(session_factory) as session:
        repository.claim_outbox_batch(session, worker_id=WORKER_A, limit=10, lease_seconds=60)
    with session_scope(session_factory) as session:
        session.scalars(select(OutboxMessage)).one().lease_expires_at = utcnow() - timedelta(
            seconds=1
        )
    with session_scope(session_factory) as session:
        assert repository.reclaim_expired_leases(session) == 1
    with session_scope(session_factory) as session:
        row = session.scalars(select(OutboxMessage)).one()
        assert row.status is OutboxStatus.FAILED
        assert row.claimed_by is None
        assert row.lease_expires_at is None
        assert "lease expired" in (row.last_error or "")


def test_reclaim_leaves_live_leases_alone(session_factory: sessionmaker[Session]) -> None:
    _queue_notifications(session_factory, 1)
    with session_scope(session_factory) as session:
        repository.claim_outbox_batch(session, worker_id=WORKER_A, limit=10, lease_seconds=3600)
    with session_scope(session_factory) as session:
        assert repository.reclaim_expired_leases(session) == 0


# ------------------------------------------------------------------ lease release


def test_a_successful_send_releases_the_lease(
    worker: OutboxWorker, transport: ScriptedTransport, session_factory: sessionmaker[Session]
) -> None:
    """A finished row must not keep a stale owner.

    Otherwise a live lease cannot be told from a completed one when reading the table.
    """
    _queue_notifications(session_factory, 1)
    assert worker.process_batch() == (1, 0)
    (row,) = _all_messages(session_factory)
    assert row.status is OutboxStatus.SENT
    assert row.claimed_by is None
    assert row.claimed_at is None
    assert row.lease_expires_at is None


def test_a_transient_failure_releases_the_lease_so_any_worker_can_retry(
    worker: OutboxWorker, transport: ScriptedTransport, session_factory: sessionmaker[Session]
) -> None:
    _queue_notifications(session_factory, 1)
    transport.fail_next_sends = 1
    assert worker.process_batch() == (0, 1)
    (row,) = _all_messages(session_factory)
    assert row.status is OutboxStatus.FAILED
    assert row.claimed_by is None, "the retry must not be reserved for the worker that failed"


def test_a_dead_lettered_message_releases_the_lease(
    worker: OutboxWorker, transport: ScriptedTransport, session_factory: sessionmaker[Session]
) -> None:
    _queue_notifications(session_factory, 1)
    transport.permanent_failure = True
    assert worker.process_batch() == (0, 1)
    (row,) = _all_messages(session_factory)
    assert row.status is OutboxStatus.DEAD
    assert row.claimed_by is None


# ------------------------------------------------------------------ real concurrency


def test_two_concurrent_workers_never_send_the_same_message_twice(
    settings: Settings, engine: Engine
) -> None:
    """The regression test for the original defect, with real threads and a real database.

    Two workers run against the same SQLite file at the same time. The transport records every
    send under a lock and sleeps briefly, which widens the window in which the old code would
    hand the same row to both workers.
    """
    factory = build_session_factory(engine)
    message_count = 12
    _queue_notifications(factory, message_count)

    lock = threading.Lock()
    sent_chat_bodies: list[str] = []

    class RecordingTransport(ScriptedTransport):
        def send(self, message: OutboundMessage) -> str:
            import time

            time.sleep(0.005)  # widen the race window
            with lock:
                sent_chat_bodies.append(message.text)
            return f"ok-{len(sent_chat_bodies)}"

    start = threading.Barrier(2)
    errors: list[BaseException] = []

    def run(worker_id: str) -> None:
        local_settings = settings.model_copy(update={"worker_id": worker_id})
        local_worker = OutboxWorker(
            settings=local_settings,
            transport=RecordingTransport(),
            session_factory=build_session_factory(engine),
        )
        try:
            start.wait(timeout=10)
            for _ in range(8):
                local_worker.process_batch()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(name,)) for name in (WORKER_A, WORKER_B)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, f"a worker raised: {errors[0]!r}"
    assert len(sent_chat_bodies) == len(set(sent_chat_bodies)), (
        "the same notification body was delivered more than once"
    )
    assert len(sent_chat_bodies) == message_count, (
        f"expected {message_count} deliveries, got {len(sent_chat_bodies)}"
    )

    rows = _all_messages(factory)
    assert len(rows) == message_count
    assert all(row.status is OutboxStatus.SENT for row in rows)
    assert all(row.claimed_by is None for row in rows)
    assert sum(row.attempts for row in rows) == message_count, (
        "each message should have been attempted exactly once"
    )


# ------------------------------------------------------------------ SQLite behaviour


def test_sqlite_busy_timeout_is_set_so_concurrent_writers_wait(engine: Engine) -> None:
    """The claim relies on SQLite serialising writers rather than failing instantly."""
    with engine.connect() as connection:
        timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar()
    assert timeout and int(timeout) >= 1000, "a busy_timeout is required for multi-worker claims"


def test_claim_is_a_single_statement(session_factory: sessionmaker[Session]) -> None:
    """Atomicity depends on the claim being one UPDATE, not a read followed by a write.

    Counting the statements the claim issues guards against someone later "simplifying" it into a
    SELECT-then-UPDATE, which would silently reintroduce the double-send bug.
    """
    _queue_notifications(session_factory, 3)
    statements: list[str] = []
    test_engine = create_engine(
        str(session_factory.kw["bind"].url), future=True, connect_args={"check_same_thread": False}
    )

    @event.listens_for(test_engine, "before_cursor_execute")
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    factory = build_session_factory(test_engine)
    with session_scope(factory) as session:
        repository.claim_outbox_batch(session, worker_id=WORKER_A, limit=3, lease_seconds=60)

    updates = [s for s in statements if s.strip().upper().startswith("UPDATE")]
    assert len(updates) == 1, f"expected exactly one UPDATE, saw {len(updates)}"
    assert "IN (SELECT" in updates[0].upper().replace("\n", " "), (
        "the claim must select its candidates inside the UPDATE, not in a separate round trip"
    )
    test_engine.dispose()


def test_sqlite_schema_has_the_lease_columns(engine: Engine) -> None:
    create_all(engine)
    with engine.connect() as connection:
        columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(outbox_messages)")
        }
    assert {"claimed_by", "claimed_at", "lease_expires_at"} <= columns
