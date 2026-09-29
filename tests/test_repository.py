"""Tests for the data layer: transitions, the audit trail, and outbox idempotency."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from talabflow import repository
from talabflow.models import (
    ALLOWED_TRANSITIONS,
    Customer,
    Order,
    OrderStatus,
    OutboxMessage,
    StaffRole,
)
from talabflow.repository import DuplicateUserError, InvalidTransitionError
from talabflow.security import PasswordPolicyError


@pytest.fixture
def customer(session: Session) -> Customer:
    return repository.get_or_create_customer(
        session, channel="scripted", channel_user_id="1001", chat_id="1001", display_name="Amina"
    )


@pytest.fixture
def order(session: Session, customer: Customer) -> Order:
    return repository.create_order(
        session,
        customer=customer,
        service_type="Repair",
        details="The washing machine will not drain",
        contact_phone="0555123456",
        address="12 Rue Didouche Mourad, Algiers",
    )


# --------------------------------------------------------------------- customers


def test_get_or_create_customer_is_idempotent(session: Session) -> None:
    first = repository.get_or_create_customer(
        session, channel="scripted", channel_user_id="7", chat_id="7", display_name="A"
    )
    second = repository.get_or_create_customer(
        session, channel="scripted", channel_user_id="7", chat_id="7", display_name="A"
    )
    assert first.id == second.id


def test_the_same_user_id_on_a_different_channel_is_a_different_customer(session: Session) -> None:
    """A Telegram user id is only unique within Telegram, so the channel is part of identity."""
    telegram = repository.get_or_create_customer(
        session, channel="telegram", channel_user_id="7", chat_id="7", display_name="A"
    )
    scripted = repository.get_or_create_customer(
        session, channel="scripted", channel_user_id="7", chat_id="7", display_name="A"
    )
    assert telegram.id != scripted.id


def test_a_changed_display_name_and_chat_id_are_picked_up(session: Session) -> None:
    created = repository.get_or_create_customer(
        session, channel="scripted", channel_user_id="7", chat_id="7", display_name="Old Name"
    )
    updated = repository.get_or_create_customer(
        session, channel="scripted", channel_user_id="7", chat_id="99", display_name="New Name"
    )
    assert updated.id == created.id
    assert updated.display_name == "New Name"
    assert updated.chat_id == "99"


# --------------------------------------------------------------------- orders


def test_create_order_allocates_a_reference_and_a_creation_event(
    order: Order, session: Session
) -> None:
    assert order.reference.startswith("TF-")
    assert order.status is OrderStatus.NEW
    events = repository.load_order_events(session, order.id)
    assert [event.to_status for event in events] == [OrderStatus.NEW]
    assert events[0].from_status is None


def test_references_are_unique_across_many_orders(session: Session, customer: Customer) -> None:
    references = {
        repository.create_order(
            session,
            customer=customer,
            service_type="Repair",
            details="details here",
            contact_phone="0555123456",
            address="an address",
        ).reference
        for _ in range(40)
    }
    assert len(references) == 40


def test_lookup_by_reference(session: Session, order: Order) -> None:
    assert repository.get_order_by_reference(session, order.reference) is not None
    assert repository.get_order_by_reference(session, "TF-20200101-AAAA") is None


def test_customer_scoped_lookup_refuses_another_customers_order(
    session: Session, order: Order
) -> None:
    other = repository.get_or_create_customer(
        session, channel="scripted", channel_user_id="8", chat_id="8", display_name="B"
    )
    assert (
        repository.get_customer_order(session, customer_id=other.id, reference=order.reference)
        is None
    )
    assert (
        repository.get_customer_order(
            session, customer_id=order.customer_id, reference=order.reference
        )
        is not None
    )


def test_list_orders_paginates_and_reports_the_true_total(
    session: Session, customer: Customer
) -> None:
    for index in range(7):
        repository.create_order(
            session,
            customer=customer,
            service_type="Repair",
            details=f"problem number {index}",
            contact_phone="0555123456",
            address="an address",
        )
    page = repository.list_orders(session, limit=3, offset=0)
    assert len(page.items) == 3
    assert page.total == 7, "total must be the unfiltered count, not the page size"


def test_list_orders_filters_by_status(session: Session, order: Order) -> None:
    repository.change_order_status(
        session, order=order, to_status=OrderStatus.CONFIRMED, actor="amina"
    )
    assert repository.list_orders(session, status=OrderStatus.CONFIRMED).total == 1
    assert repository.list_orders(session, status=OrderStatus.NEW).total == 0


def test_search_matches_reference_details_and_phone(session: Session, order: Order) -> None:
    for needle in [order.reference, "washing machine", "0555123456", "Didouche"]:
        assert repository.list_orders(session, search=needle).total == 1, needle
    assert repository.list_orders(session, search="definitely-absent").total == 0


def test_search_treats_sql_wildcards_as_literal_text(session: Session, order: Order) -> None:
    """A search for "%" must not match everything: the value stays a bound parameter."""
    assert repository.list_orders(session, search="'; DROP TABLE orders; --").total == 0
    # And the table is still there afterwards.
    assert repository.list_orders(session).total == 1


def test_counts_include_statuses_with_no_orders(session: Session, order: Order) -> None:
    counts = repository.count_orders_by_status(session)
    assert counts["new"] == 1
    assert counts["completed"] == 0
    assert set(counts) == {status.value for status in OrderStatus}


# --------------------------------------------------------------------- transitions


def test_the_full_pipeline_can_be_walked(session: Session, order: Order) -> None:
    for target in [
        OrderStatus.CONFIRMED,
        OrderStatus.IN_PROGRESS,
        OrderStatus.READY,
        OrderStatus.COMPLETED,
    ]:
        repository.change_order_status(session, order=order, to_status=target, actor="amina")
        assert order.status is target


def test_skipping_a_step_is_refused_with_a_helpful_message(session: Session, order: Order) -> None:
    with pytest.raises(InvalidTransitionError) as info:
        repository.change_order_status(
            session, order=order, to_status=OrderStatus.COMPLETED, actor="amina"
        )
    message = str(info.value)
    assert "from new to completed" in message
    # The error names what *is* allowed, so a client never has to guess.
    assert "confirmed" in message
    assert order.status is OrderStatus.NEW, "a refused transition must not mutate the order"


def test_cancellation_is_possible_from_every_non_terminal_status(
    session: Session, customer: Customer
) -> None:
    for status in [
        OrderStatus.NEW,
        OrderStatus.CONFIRMED,
        OrderStatus.IN_PROGRESS,
        OrderStatus.READY,
    ]:
        assert OrderStatus.CANCELLED in ALLOWED_TRANSITIONS[status], status


def test_terminal_statuses_allow_nothing_further(session: Session, order: Order) -> None:
    repository.change_order_status(
        session, order=order, to_status=OrderStatus.CANCELLED, actor="amina"
    )
    assert order.status.is_terminal
    with pytest.raises(InvalidTransitionError, match="terminal"):
        repository.change_order_status(
            session, order=order, to_status=OrderStatus.CONFIRMED, actor="amina"
        )


def test_every_change_appends_to_the_audit_trail(session: Session, order: Order) -> None:
    repository.change_order_status(
        session, order=order, to_status=OrderStatus.CONFIRMED, actor="amina", note="Tech assigned"
    )
    repository.change_order_status(
        session, order=order, to_status=OrderStatus.IN_PROGRESS, actor="karim"
    )
    events = repository.load_order_events(session, order.id)
    assert [event.to_status.value for event in events] == ["new", "confirmed", "in_progress"]
    assert [event.actor for event in events] == ["customer", "amina", "karim"]
    assert events[1].note == "Tech assigned"
    assert events[1].from_status is OrderStatus.NEW


# --------------------------------------------------------------------- outbox


def _outbox(session: Session) -> list[OutboxMessage]:
    from sqlalchemy import select

    return list(session.scalars(select(OutboxMessage).order_by(OutboxMessage.id)))


def test_a_status_change_queues_exactly_one_notification(session: Session, order: Order) -> None:
    repository.change_order_status(
        session, order=order, to_status=OrderStatus.CONFIRMED, actor="amina"
    )
    messages = _outbox(session)
    assert len(messages) == 1
    assert order.reference in messages[0].body
    assert messages[0].chat_id == order.customer.chat_id


def test_order_creation_does_not_queue_a_notification(session: Session, order: Order) -> None:
    """The customer is answered in the conversation itself; a second message would be noise."""
    assert _outbox(session) == []


def test_notify_false_skips_the_notification(session: Session, order: Order) -> None:
    repository.change_order_status(
        session, order=order, to_status=OrderStatus.CONFIRMED, actor="amina", notify=False
    )
    assert _outbox(session) == []


def test_queueing_the_same_event_twice_is_idempotent(session: Session, order: Order) -> None:
    """The property that stops a customer being messaged twice for one change."""
    event = repository.change_order_status(
        session, order=order, to_status=OrderStatus.CONFIRMED, actor="amina"
    )
    assert len(_outbox(session)) == 1
    again = repository.queue_status_notification(session, order=order, event=event)
    assert again is None, "a duplicate enqueue must be a no-op, not a second message"
    assert len(_outbox(session)) == 1


def test_each_distinct_change_gets_its_own_notification(session: Session, order: Order) -> None:
    repository.change_order_status(
        session, order=order, to_status=OrderStatus.CONFIRMED, actor="amina"
    )
    repository.change_order_status(
        session, order=order, to_status=OrderStatus.IN_PROGRESS, actor="amina"
    )
    messages = _outbox(session)
    assert len(messages) == 2
    assert len({message.idempotency_key for message in messages}) == 2


def test_the_idempotency_key_is_derived_from_the_event(session: Session, order: Order) -> None:
    event = repository.change_order_status(
        session, order=order, to_status=OrderStatus.CONFIRMED, actor="amina"
    )
    assert _outbox(session)[0].idempotency_key == repository.outbox_key_for_event(event.id)


def test_a_staff_note_reaches_the_customer_but_the_creation_note_does_not(
    session: Session, order: Order
) -> None:
    repository.change_order_status(
        session,
        order=order,
        to_status=OrderStatus.CONFIRMED,
        actor="amina",
        note="A technician will call you",
    )
    body = _outbox(session)[0].body
    assert "A technician will call you" in body
    assert "order created" not in body


def test_due_messages_are_claimed_oldest_first(session: Session, order: Order) -> None:
    repository.change_order_status(
        session, order=order, to_status=OrderStatus.CONFIRMED, actor="amina"
    )
    repository.change_order_status(
        session, order=order, to_status=OrderStatus.IN_PROGRESS, actor="amina"
    )
    due = repository.due_outbox_messages(session, limit=10)
    assert [message.id for message in due] == sorted(message.id for message in due)


def test_claiming_respects_the_batch_limit(session: Session, order: Order) -> None:
    repository.change_order_status(
        session, order=order, to_status=OrderStatus.CONFIRMED, actor="amina"
    )
    repository.change_order_status(
        session, order=order, to_status=OrderStatus.IN_PROGRESS, actor="amina"
    )
    assert len(repository.due_outbox_messages(session, limit=1)) == 1


# --------------------------------------------------------------------- staff


def test_create_and_fetch_staff_user(session: Session) -> None:
    user = repository.create_staff_user(
        session, username="Amina", password="a-good-password", role=StaffRole.ADMIN
    )
    assert user.username == "amina", "usernames are normalised to lower case"
    assert user.role is StaffRole.ADMIN
    assert repository.get_staff_user(session, "AMINA") is not None


def test_the_password_is_not_stored_in_clear_text(session: Session) -> None:
    user = repository.create_staff_user(session, username="amina", password="a-good-password")
    assert "a-good-password" not in user.password_hash
    assert user.password_hash.startswith("scrypt$")


def test_duplicate_usernames_are_refused(session: Session) -> None:
    repository.create_staff_user(session, username="amina", password="a-good-password")
    with pytest.raises(DuplicateUserError):
        repository.create_staff_user(session, username="AMINA", password="another-password")


def test_a_short_password_is_refused(session: Session) -> None:
    with pytest.raises(PasswordPolicyError):
        repository.create_staff_user(session, username="amina", password="short")


def test_an_empty_username_is_refused(session: Session) -> None:
    with pytest.raises(ValueError, match="username must not be empty"):
        repository.create_staff_user(session, username="   ", password="a-good-password")


def test_the_repr_of_a_staff_user_does_not_leak_the_hash(session: Session) -> None:
    """A repr can end up in a log line or a traceback."""
    user = repository.create_staff_user(session, username="amina", password="a-good-password")
    assert "scrypt" not in repr(user)
    assert user.password_hash not in repr(user)
