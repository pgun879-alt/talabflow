"""Tests for the intake state machine.

These are the tests that matter most in this project: the state machine is what a customer
actually touches, and every branch here is a way a real person derails a scripted bot.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from talabflow import repository
from talabflow.conversation import (
    MIN_DETAILS_LENGTH,
    ConversationEngine,
    Step,
    normalise_phone,
)
from talabflow.models import Customer, OrderStatus


@pytest.fixture
def customer(session: Session) -> Customer:
    return repository.get_or_create_customer(
        session,
        channel="scripted",
        channel_user_id="1001",
        chat_id="1001",
        display_name="Test Customer",
    )


def _state(session: Session, customer: Customer):
    return repository.get_conversation_state(session, customer, default_step=Step.IDLE.value)


def _say(engine: ConversationEngine, session: Session, customer: Customer, text: str) -> list[str]:
    state = _state(session, customer)
    return engine.handle(session, customer=customer, state=state, text=text).texts


# --------------------------------------------------------------------- phone parsing


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0555123456", "0555123456"),
        ("0555 12 34 56", "0555123456"),
        ("+213 (555) 123-456", "+213555123456"),
        ("0555-12-34-56", "0555123456"),
        ("  0555123456  ", "0555123456"),
    ],
)
def test_normalise_phone_accepts_real_world_formatting(raw: str, expected: str) -> None:
    """People write numbers many ways; rejecting formatting rejects paying customers."""
    assert normalise_phone(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "call me maybe",
        "123",  # too few digits
        "1234567890123456789",  # too many
        "",
        "   ",
        "0555abc456",
        "<script>alert(1)</script>",
    ],
)
def test_normalise_phone_rejects_non_numbers(raw: str) -> None:
    assert normalise_phone(raw) is None


# --------------------------------------------------------------------- happy path


def test_full_happy_path_creates_an_order(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    _say(conversation_engine, session, customer, "/new")
    _say(conversation_engine, session, customer, "1")
    _say(conversation_engine, session, customer, "The washing machine will not drain")
    _say(conversation_engine, session, customer, "0555123456")
    _say(conversation_engine, session, customer, "12 Rue Didouche Mourad, Algiers")

    state = _state(session, customer)
    result = conversation_engine.handle(session, customer=customer, state=state, text="yes")

    assert result.created_order is not None
    order = result.created_order
    assert order.service_type == "Repair"
    assert order.contact_phone == "0555123456"
    assert order.status is OrderStatus.NEW
    assert order.reference.startswith("TF-")
    assert order.reference in result.texts[0]
    # The draft is cleared and the customer is back at rest, ready for a second order.
    assert state.step == Step.IDLE.value
    assert state.draft_service_type is None


def test_the_creation_event_is_recorded(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    for text in ["/new", "1", "Machine will not drain", "0555123456", "Some street, Algiers"]:
        _say(conversation_engine, session, customer, text)
    state = _state(session, customer)
    order = conversation_engine.handle(
        session, customer=customer, state=state, text="yes"
    ).created_order
    assert order is not None
    events = repository.load_order_events(session, order.id)
    assert len(events) == 1
    assert events[0].from_status is None
    assert events[0].to_status is OrderStatus.NEW
    assert events[0].actor == "customer"


def test_service_can_be_chosen_by_name_not_only_by_number(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    """Customers reply "installation" as often as they reply "2"."""
    _say(conversation_engine, session, customer, "/new")
    replies = _say(conversation_engine, session, customer, "installation")
    assert "Installation" in replies[0]
    assert _state(session, customer).draft_service_type == "Installation"


def test_the_phone_is_saved_onto_the_customer_record(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    for text in [
        "/new",
        "1",
        "Machine will not drain",
        "0555123456",
        "Some street, Algiers",
        "yes",
    ]:
        _say(conversation_engine, session, customer, text)
    assert customer.phone == "0555123456"


# --------------------------------------------------------------------- commands


def test_start_and_help_show_the_menu(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    for command in ("/start", "/help"):
        replies = _say(conversation_engine, session, customer, command)
        assert "/new" in replies[0]
        assert "/status" in replies[0]


def test_telegram_group_style_command_mention_is_stripped(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    """In groups Telegram sends "/start@YourBot"; that must still be recognised."""
    replies = _say(conversation_engine, session, customer, "/start@TalabflowBot")
    assert "/new" in replies[0]


def test_commands_work_from_the_middle_of_an_order(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    """A customer stuck mid-flow must always be able to escape. This is why commands are
    handled before step logic rather than inside it."""
    _say(conversation_engine, session, customer, "/new")
    _say(conversation_engine, session, customer, "1")
    replies = _say(conversation_engine, session, customer, "/help")
    assert "/new" in replies[0]
    # ...and the draft survives the detour.
    assert _state(session, customer).draft_service_type == "Repair"


def test_cancel_discards_the_draft(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    _say(conversation_engine, session, customer, "/new")
    _say(conversation_engine, session, customer, "1")
    _say(conversation_engine, session, customer, "Some details here")
    replies = _say(conversation_engine, session, customer, "/cancel")
    assert "discarded" in replies[0]
    state = _state(session, customer)
    assert state.step == Step.IDLE.value
    assert state.draft_service_type is None
    assert state.draft_details is None


def test_cancel_with_nothing_in_progress_says_so(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    replies = _say(conversation_engine, session, customer, "/cancel")
    assert "no order in progress" in replies[0]


def test_new_restarts_a_half_finished_order(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    _say(conversation_engine, session, customer, "/new")
    _say(conversation_engine, session, customer, "1")
    _say(conversation_engine, session, customer, "Some details here")
    _say(conversation_engine, session, customer, "/new")
    state = _state(session, customer)
    assert state.step == Step.AWAITING_SERVICE.value
    assert state.draft_details is None, "the old draft must not leak into the new order"


def test_unknown_command_is_handled(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    replies = _say(conversation_engine, session, customer, "/definitely-not-a-command")
    assert "did not understand" in replies[0]


def test_free_text_while_idle_is_redirected_to_help(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    replies = _say(conversation_engine, session, customer, "hello is anyone there")
    assert "/help" in replies[0]


# --------------------------------------------------------------------- validation


def test_invalid_service_number_re_prompts_without_advancing(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    _say(conversation_engine, session, customer, "/new")
    for bad in ["99", "0", "-1", "banana"]:
        replies = _say(conversation_engine, session, customer, bad)
        assert "one of the numbers" in replies[0]
        assert _state(session, customer).step == Step.AWAITING_SERVICE.value


def test_too_short_details_re_prompt(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    _say(conversation_engine, session, customer, "/new")
    _say(conversation_engine, session, customer, "1")
    replies = _say(conversation_engine, session, customer, "x")
    assert str(MIN_DETAILS_LENGTH) in replies[0]
    assert _state(session, customer).step == Step.AWAITING_DETAILS.value


def test_invalid_phone_re_prompts(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    _say(conversation_engine, session, customer, "/new")
    _say(conversation_engine, session, customer, "1")
    _say(conversation_engine, session, customer, "Machine will not drain")
    replies = _say(conversation_engine, session, customer, "call me on my mobile")
    assert "phone number" in replies[0]
    assert _state(session, customer).step == Step.AWAITING_PHONE.value


def test_an_over_long_message_is_refused(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    replies = _say(conversation_engine, session, customer, "x" * 5000)
    assert "too long" in replies[0]


def test_an_empty_message_does_not_crash(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    assert _say(conversation_engine, session, customer, "   ")


def test_confirmation_requires_an_affirmative(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    for text in ["/new", "1", "Machine will not drain", "0555123456", "Some street, Algiers"]:
        _say(conversation_engine, session, customer, text)
    replies = _say(conversation_engine, session, customer, "maybe later")
    assert "YES" in replies[0]
    assert _state(session, customer).step == Step.CONFIRMING.value
    assert repository.list_orders(session).total == 0, "nothing should be saved yet"


@pytest.mark.parametrize("affirmative", ["yes", "YES", "Yes!", "ok", "confirm", "y"])
def test_affirmative_variants_are_accepted(
    conversation_engine: ConversationEngine,
    session: Session,
    customer: Customer,
    affirmative: str,
) -> None:
    for text in ["/new", "1", "Machine will not drain", "0555123456", "Some street, Algiers"]:
        _say(conversation_engine, session, customer, text)
    state = _state(session, customer)
    result = conversation_engine.handle(session, customer=customer, state=state, text=affirmative)
    assert result.created_order is not None


def test_arabic_affirmative_is_accepted(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    for text in ["/new", "1", "Machine will not drain", "0555123456", "Some street, Algiers"]:
        _say(conversation_engine, session, customer, text)
    state = _state(session, customer)
    result = conversation_engine.handle(session, customer=customer, state=state, text="نعم")
    assert result.created_order is not None


# --------------------------------------------------------------------- status command


def test_status_lookup_works_and_tolerates_sloppy_references(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    for text in ["/new", "1", "Machine will not drain", "0555123456", "Some street, Algiers"]:
        _say(conversation_engine, session, customer, text)
    state = _state(session, customer)
    order = conversation_engine.handle(
        session, customer=customer, state=state, text="yes"
    ).created_order
    assert order is not None

    for variant in [order.reference, order.reference.lower(), order.reference.replace("-", " ")]:
        replies = _say(conversation_engine, session, customer, f"/status {variant}")
        assert order.reference in replies[0], variant


def test_status_without_a_reference_explains_the_usage(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    replies = _say(conversation_engine, session, customer, "/status")
    assert "/status TF-" in replies[0]


def test_status_for_an_unknown_reference_is_not_found(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    replies = _say(conversation_engine, session, customer, "/status TF-20260101-ZZZZ")
    assert "could not find" in replies[0]


def test_one_customer_cannot_read_another_customers_order(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    """The security control behind /status.

    Without customer scoping, anyone who overheard or guessed a reference could read the
    order's phone number and home address.
    """
    for text in ["/new", "1", "Machine will not drain", "0555123456", "Secret Street 9, Algiers"]:
        _say(conversation_engine, session, customer, text)
    state = _state(session, customer)
    order = conversation_engine.handle(
        session, customer=customer, state=state, text="yes"
    ).created_order
    assert order is not None

    intruder = repository.get_or_create_customer(
        session, channel="scripted", channel_user_id="9999", chat_id="9999", display_name="Nosy"
    )
    replies = _say(conversation_engine, session, intruder, f"/status {order.reference}")
    assert "could not find" in replies[0]
    assert "Secret Street" not in replies[0]
    assert order.contact_phone not in replies[0]


# --------------------------------------------------------------------- edge cases


def test_a_blocked_customer_cannot_place_orders(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    customer.is_blocked = True
    replies = _say(conversation_engine, session, customer, "/new")
    assert "cannot place orders" in replies[0]
    assert repository.list_orders(session).total == 0


def test_an_unknown_persisted_step_resets_instead_of_looping(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    """A schema change under a live conversation must not trap the customer forever."""
    state = _state(session, customer)
    state.step = "a_step_that_no_longer_exists"
    replies = conversation_engine.handle(
        session, customer=customer, state=state, text="hello"
    ).texts
    assert replies
    assert state.step == Step.IDLE.value


def test_reaching_confirmation_with_an_incomplete_draft_restarts_cleanly(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    """Defensive path: a bug upstream must not create a half-empty order."""
    state = _state(session, customer)
    state.step = Step.CONFIRMING.value
    state.draft_service_type = "Repair"  # details, phone and address deliberately missing
    result = conversation_engine.handle(session, customer=customer, state=state, text="yes")
    assert result.created_order is None
    assert state.step == Step.AWAITING_SERVICE.value
    assert repository.list_orders(session).total == 0


def test_two_customers_have_independent_conversations(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    other = repository.get_or_create_customer(
        session, channel="scripted", channel_user_id="2002", chat_id="2002", display_name="Other"
    )
    _say(conversation_engine, session, customer, "/new")
    _say(conversation_engine, session, customer, "1")
    # The second customer starts from scratch, unaffected by the first.
    assert _state(session, other).step == Step.IDLE.value
    _say(conversation_engine, session, other, "/new")
    _say(conversation_engine, session, other, "3")
    assert _state(session, customer).draft_service_type == "Repair"
    assert _state(session, other).draft_service_type == "Consultation"


def test_conversation_state_survives_a_new_engine_instance(
    settings, session: Session, customer: Customer
) -> None:
    """State is persisted, not in memory, so a restart does not lose a half-finished order."""
    first = ConversationEngine(services=settings.service_types, business_name="X")
    _say(first, session, customer, "/new")
    _say(first, session, customer, "1")
    _say(first, session, customer, "The washing machine will not drain")

    second = ConversationEngine(services=settings.service_types, business_name="X")
    replies = _say(second, session, customer, "0555123456")
    assert "address" in replies[0].lower()


def test_engine_requires_at_least_one_service() -> None:
    with pytest.raises(ValueError, match="at least one service"):
        ConversationEngine(services=(), business_name="X")
