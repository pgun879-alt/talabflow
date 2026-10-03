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
    # Stored in one canonical form, whatever way it was typed.
    assert order.contact_phone == "+213555123456"
    assert order.contact_phone_verified is False
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
    assert customer.phone == "+213555123456"


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


@pytest.mark.parametrize("text", ["²", "①", "⁴²", pytest.param("9" * 5000, id="5000-digits")])
def test_digit_like_input_that_is_not_a_number_re_prompts_instead_of_raising(
    conversation_engine: ConversationEngine, session: Session, customer: Customer, text: str
) -> None:
    """Regression guard: ``"²".isdigit()`` is true, but ``int("²")`` raises.

    The exception escaped the handler, so the customer got no reply at all -- exactly what the
    engine promises never to do for bad input. A very long run of digits fails the same way, on
    Python's integer-conversion length limit.
    """
    long_engine = ConversationEngine(
        services=conversation_engine.services, business_name="X", max_message_length=8000
    )
    _say(long_engine, session, customer, "/new")
    replies = _say(long_engine, session, customer, text)
    assert "1. Repair" in replies[0]
    assert _state(session, customer).step == Step.AWAITING_SERVICE.value


def test_the_service_menu_accepts_arabic_indic_digits(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    _say(conversation_engine, session, customer, "/new")
    _say(conversation_engine, session, customer, "٢")
    assert _state(session, customer).draft_service_type == conversation_engine.services[1]


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
    first = ConversationEngine(
        services=settings.service_types, business_name="X", phone_default_region="DZ"
    )
    _say(first, session, customer, "/new")
    _say(first, session, customer, "1")
    _say(first, session, customer, "The washing machine will not drain")

    second = ConversationEngine(
        services=settings.service_types, business_name="X", phone_default_region="DZ"
    )
    replies = _say(second, session, customer, "0555123456")
    assert "address" in replies[0].lower()


def test_engine_requires_at_least_one_service() -> None:
    with pytest.raises(ValueError, match="at least one service"):
        ConversationEngine(services=(), business_name="X")


# --------------------------------------------------------------------- phone validation


def _to_phone_step(engine: ConversationEngine, session: Session, customer: Customer) -> None:
    for text in ["/new", "1", "The washing machine will not drain"]:
        _say(engine, session, customer, text)
    assert _state(session, customer).step == Step.AWAITING_PHONE.value


def _turn(engine: ConversationEngine, session: Session, customer: Customer, text: str, **flags):
    state = _state(session, customer)
    return engine.handle(session, customer=customer, state=state, text=text, **flags)


@pytest.mark.parametrize("made_up", ["98765432109876", "+98765432109876", "0155123456", "12345"])
def test_a_made_up_number_is_refused_at_the_phone_step(
    conversation_engine: ConversationEngine, session: Session, customer: Customer, made_up: str
) -> None:
    """Regression guard. ``98765432109876`` was accepted in a manual test: the old check only
    counted digits, and fourteen is inside the 8-to-15 range."""
    _to_phone_step(conversation_engine, session, customer)
    replies = _say(conversation_engine, session, customer, made_up)
    state = _state(session, customer)
    assert state.step == Step.AWAITING_PHONE.value, "the bot moved on with a number nobody can call"
    assert state.draft_phone is None
    assert "address" not in replies[0].lower()


def test_a_refused_number_is_answered_with_an_example_of_a_valid_one(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    """ "Invalid number" alone leaves the customer guessing what would be accepted."""
    _to_phone_step(conversation_engine, session, customer)
    reply = _say(conversation_engine, session, customer, "98765432109876")[0]
    assert "0551 23 45 67" in reply
    assert "+213 551 23 45 67" in reply


def test_a_number_from_another_country_is_accepted_with_its_country_code(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    _to_phone_step(conversation_engine, session, customer)
    replies = _say(conversation_engine, session, customer, "+212 612-345678")
    assert "address" in replies[0].lower()
    assert _state(session, customer).draft_phone == "+212612345678"


def test_the_confirmation_reads_the_number_back_grouped(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    """A mistyped digit is far easier to spot in ``+213 555 12 34 56`` than in a run of ten."""
    _to_phone_step(conversation_engine, session, customer)
    _say(conversation_engine, session, customer, "0555123456")
    replies = _say(conversation_engine, session, customer, "12 Rue Didouche Mourad, Algiers")
    assert "+213 555 12 34 56" in replies[0]


def test_without_a_default_region_the_bot_asks_for_the_country_code(
    settings, session: Session, customer: Customer
) -> None:
    engine = ConversationEngine(services=settings.service_types, business_name="X")
    _to_phone_step(engine, session, customer)
    reply = _say(engine, session, customer, "0555123456")[0]
    assert "country code" in reply
    assert _state(session, customer).step == Step.AWAITING_PHONE.value

    assert "address" in _say(engine, session, customer, "+213555123456")[0].lower()


def test_without_a_default_region_no_local_style_example_is_offered(
    settings, session: Session, customer: Customer
) -> None:
    """A local-style example would itself be refused when no country is configured."""
    engine = ConversationEngine(services=settings.service_types, business_name="X")
    _to_phone_step(engine, session, customer)
    reply = _say(engine, session, customer, "not a number")[0]
    assert "+213 551 23 45 67" in reply
    assert "0551 23 45 67" not in reply


def test_a_number_outside_the_allowed_countries_is_refused_and_the_countries_are_named(
    settings, session: Session, customer: Customer
) -> None:
    engine = ConversationEngine(
        services=settings.service_types,
        business_name="X",
        phone_default_region="DZ",
        phone_allowed_regions=("DZ", "MA"),
    )
    _to_phone_step(engine, session, customer)
    reply = _say(engine, session, customer, "+33 6 12 34 56 78")[0]
    assert "DZ (+213), MA (+212)" in reply
    assert _state(session, customer).step == Step.AWAITING_PHONE.value

    assert "address" in _say(engine, session, customer, "+212 612-345678")[0].lower()


def test_the_refusal_messages_exist_in_arabic(
    settings, session: Session, customer: Customer
) -> None:
    engine = ConversationEngine(
        services=settings.service_types,
        business_name="X",
        language="ar",
        phone_default_region="DZ",
        phone_allowed_regions=("DZ",),
    )
    _to_phone_step(engine, session, customer)
    assert "0551 23 45 67" in _say(engine, session, customer, "98765432109876")[0]
    assert "DZ (+213)" in _say(engine, session, customer, "+212612345678")[0]


# --------------------------------------------------------------------- shared contact


def test_sharing_your_own_contact_records_a_verified_number(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    """The messaging app sends the customer's own number in international form with no "+"."""
    _to_phone_step(conversation_engine, session, customer)
    result = _turn(
        conversation_engine,
        session,
        customer,
        "213555123456",
        shared_contact=True,
        contact_is_own=True,
    )
    assert "address" in result.texts[0].lower()
    _say(conversation_engine, session, customer, "12 Rue Didouche Mourad, Algiers")
    order = _turn(conversation_engine, session, customer, "yes").created_order
    assert order is not None
    assert order.contact_phone == "+213555123456"
    assert order.contact_phone_verified is True


def test_your_own_contact_from_another_country_is_not_misread_as_a_local_number(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    """``212612345678`` must be read as +212..., not as an Algerian number that happens to be
    twelve digits long."""
    _to_phone_step(conversation_engine, session, customer)
    _turn(
        conversation_engine,
        session,
        customer,
        "212612345678",
        shared_contact=True,
        contact_is_own=True,
    )
    state = _state(session, customer)
    assert state.draft_phone == "+212612345678"
    assert state.draft_phone_verified is True


def test_somebody_elses_contact_card_is_accepted_but_not_verified(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    """A customer may want to be called on a relative's number. That is fine -- it just is not
    proof of anything, so it must not be marked verified."""
    _to_phone_step(conversation_engine, session, customer)
    _turn(conversation_engine, session, customer, "0555 12 34 56", shared_contact=True)
    state = _state(session, customer)
    assert state.draft_phone == "+213555123456"
    assert state.draft_phone_verified is False


def test_somebody_elses_card_saved_without_a_plus_sign_is_still_understood(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    _to_phone_step(conversation_engine, session, customer)
    _turn(conversation_engine, session, customer, "212612345678", shared_contact=True)
    state = _state(session, customer)
    assert state.draft_phone == "+212612345678"
    assert state.draft_phone_verified is False


def test_a_typed_number_is_never_verified(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    """``contact_is_own`` without a shared contact card means nothing: verification comes from
    the card, never from text."""
    _to_phone_step(conversation_engine, session, customer)
    _turn(conversation_engine, session, customer, "0555123456", contact_is_own=True)
    assert _state(session, customer).draft_phone_verified is False


def test_a_shared_contact_still_has_to_be_a_valid_number(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    _to_phone_step(conversation_engine, session, customer)
    _turn(
        conversation_engine,
        session,
        customer,
        "98765432109876",
        shared_contact=True,
        contact_is_own=True,
    )
    state = _state(session, customer)
    assert state.step == Step.AWAITING_PHONE.value
    assert state.draft_phone is None
    assert state.draft_phone_verified is False


def test_a_shared_contact_must_respect_the_allowed_countries(
    settings, session: Session, customer: Customer
) -> None:
    engine = ConversationEngine(
        services=settings.service_types,
        business_name="X",
        phone_default_region="DZ",
        phone_allowed_regions=("DZ",),
    )
    _to_phone_step(engine, session, customer)
    result = _turn(
        engine, session, customer, "33612345678", shared_contact=True, contact_is_own=True
    )
    assert "DZ (+213)" in result.texts[0]
    assert _state(session, customer).step == Step.AWAITING_PHONE.value


def test_the_verified_flag_does_not_leak_into_the_next_order(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    """Share a contact, abandon the order, start again and type a number: the second order's
    number was typed, so it is not verified."""
    _to_phone_step(conversation_engine, session, customer)
    _turn(
        conversation_engine,
        session,
        customer,
        "213555123456",
        shared_contact=True,
        contact_is_own=True,
    )
    _say(conversation_engine, session, customer, "/cancel")

    _to_phone_step(conversation_engine, session, customer)
    _say(conversation_engine, session, customer, "0770 12 34 56")
    _say(conversation_engine, session, customer, "12 Rue Didouche Mourad, Algiers")
    order = _turn(conversation_engine, session, customer, "yes").created_order
    assert order is not None
    assert order.contact_phone == "+213770123456"
    assert order.contact_phone_verified is False


def test_a_contact_card_sent_outside_the_phone_step_changes_nothing(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    result = _turn(
        conversation_engine,
        session,
        customer,
        "213555123456",
        shared_contact=True,
        contact_is_own=True,
    )
    assert result.created_order is None
    state = _state(session, customer)
    assert state.step == Step.IDLE.value
    assert state.draft_phone is None


# --------------------------------------------------------------------- the share button


def test_the_share_button_is_offered_when_the_phone_is_asked_for(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    _say(conversation_engine, session, customer, "/new")
    _say(conversation_engine, session, customer, "1")
    result = _turn(conversation_engine, session, customer, "The washing machine will not drain")
    assert result.replies[-1].contact_button == "Share my phone number"
    assert result.replies[-1].remove_keyboard is False


def test_the_share_button_is_offered_again_after_a_refused_number(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    """Someone whose typed number was just refused is exactly who needs the one-tap way."""
    _to_phone_step(conversation_engine, session, customer)
    result = _turn(conversation_engine, session, customer, "98765432109876")
    assert result.replies[-1].contact_button == "Share my phone number"


def test_the_share_button_is_taken_away_once_the_phone_step_is_over(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    _to_phone_step(conversation_engine, session, customer)
    result = _turn(conversation_engine, session, customer, "0555123456")
    assert result.replies[0].remove_keyboard is True
    assert result.replies[0].contact_button is None


def test_cancelling_at_the_phone_step_also_takes_the_button_away(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    """Otherwise the button would sit under the chat for a customer who is no longer ordering."""
    _to_phone_step(conversation_engine, session, customer)
    result = _turn(conversation_engine, session, customer, "/cancel")
    assert result.replies[0].remove_keyboard is True


def test_no_other_step_shows_or_removes_a_keyboard(
    conversation_engine: ConversationEngine, session: Session, customer: Customer
) -> None:
    for text in ["/start", "/new", "1"]:
        for reply in _turn(conversation_engine, session, customer, text).replies:
            assert reply.contact_button is None
            assert reply.remove_keyboard is False


def test_the_share_button_label_follows_the_bot_language(
    settings, session: Session, customer: Customer
) -> None:
    engine = ConversationEngine(services=settings.service_types, business_name="X", language="ar")
    _say(engine, session, customer, "/new")
    _say(engine, session, customer, "1")
    result = _turn(engine, session, customer, "الغسالة لا تصرف الماء أبدا")
    assert result.replies[-1].contact_button == "مشاركة رقم هاتفي"
