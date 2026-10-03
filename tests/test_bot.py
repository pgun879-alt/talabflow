"""Tests for the bot runner: flood control, error resilience, and the send-after-commit rule."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker

from talabflow import repository
from talabflow.bot import BotRunner
from talabflow.config import Settings
from talabflow.db import session_scope
from talabflow.models import OrderStatus
from talabflow.transports.base import InboundMessage, TransportError
from talabflow.transports.scripted import ScriptedTransport

from .conftest import HAPPY_PATH


def test_the_whole_order_flow_works_through_the_runner(
    bot: BotRunner, transport: ScriptedTransport, session_factory: sessionmaker[Session]
) -> None:
    transport.queue_many(HAPPY_PATH)
    handled = bot.poll_once()
    assert handled == len(HAPPY_PATH)

    with session_scope(session_factory) as session:
        page = repository.list_orders(session)
        assert page.total == 1
        order = page.items[0]
        assert order.service_type == "Repair"
        assert order.contact_phone == "+213555123456"
        assert order.reference in transport.last_text()


def test_a_customer_record_is_created_from_the_message_metadata(
    bot: BotRunner, transport: ScriptedTransport, session_factory: sessionmaker[Session]
) -> None:
    transport.queue("/start", user_id="555", chat_id="999", display_name="Karim T")
    bot.poll_once()
    with session_scope(session_factory) as session:
        customer = repository.get_or_create_customer(
            session,
            channel="scripted",
            channel_user_id="555",
            chat_id="999",
            display_name="Karim T",
        )
        assert customer.display_name == "Karim T"
        assert customer.chat_id == "999"


def test_replies_go_to_the_chat_the_message_came_from(
    bot: BotRunner, transport: ScriptedTransport
) -> None:
    transport.queue("/start", user_id="555", chat_id="999")
    bot.poll_once()
    assert transport.sent[-1].chat_id == "999"


def test_two_customers_are_served_independently_in_one_batch(
    bot: BotRunner, transport: ScriptedTransport, session_factory: sessionmaker[Session]
) -> None:
    transport.queue_many(HAPPY_PATH, user_id="1001")
    transport.queue_many(HAPPY_PATH, user_id="2002")
    bot.poll_once()
    with session_scope(session_factory) as session:
        assert repository.list_orders(session).total == 2


# --------------------------------------------------------------------- flood control


def test_flood_control_refuses_a_customer_sending_too_fast(
    settings: Settings, transport: ScriptedTransport, session_factory: sessionmaker[Session]
) -> None:
    tight = settings.model_copy(update={"user_messages_per_minute": 3})
    runner = BotRunner(settings=tight, transport=transport, session_factory=session_factory)
    transport.queue_many(["/start"] * 6)
    runner.poll_once()
    texts = transport.sent_texts()
    assert any("very quickly" in text for text in texts)
    assert sum("very quickly" in text for text in texts) == 3


def test_flood_control_is_per_customer(
    settings: Settings, transport: ScriptedTransport, session_factory: sessionmaker[Session]
) -> None:
    """One noisy customer must not silence everyone else."""
    tight = settings.model_copy(update={"user_messages_per_minute": 2})
    runner = BotRunner(settings=tight, transport=transport, session_factory=session_factory)
    transport.queue_many(["/start"] * 4, user_id="1001")
    transport.queue("/start", user_id="2002")
    runner.poll_once()
    for message in transport.sent:
        if message.chat_id == "2002":
            assert "very quickly" not in message.text


def test_a_flood_limited_message_does_not_create_an_order(
    settings: Settings, transport: ScriptedTransport, session_factory: sessionmaker[Session]
) -> None:
    tight = settings.model_copy(update={"user_messages_per_minute": 1})
    runner = BotRunner(settings=tight, transport=transport, session_factory=session_factory)
    transport.queue_many(HAPPY_PATH)
    runner.poll_once()
    with session_scope(session_factory) as session:
        assert repository.list_orders(session).total == 0


# --------------------------------------------------------------------- resilience


def test_a_poll_failure_is_absorbed(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    class BrokenPoll(ScriptedTransport):
        def poll(self, *, timeout_seconds: float) -> list[InboundMessage]:
            raise TransportError("the network is down")

    runner = BotRunner(settings=settings, transport=BrokenPoll(), session_factory=session_factory)
    assert runner.poll_once() == 0, "a transport failure must not raise out of the loop"


def test_a_send_failure_does_not_lose_the_order(
    settings: Settings, transport: ScriptedTransport, session_factory: sessionmaker[Session]
) -> None:
    """The order is committed before any reply is attempted, so a delivery failure on the
    confirmation cannot roll back a saved order."""
    transport.queue_many(HAPPY_PATH)
    transport.fail_next_sends = 99
    runner = BotRunner(settings=settings, transport=transport, session_factory=session_factory)
    runner.poll_once()
    with session_scope(session_factory) as session:
        assert repository.list_orders(session).total == 1


def test_one_failing_message_does_not_stop_the_batch(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    class Exploding(ScriptedTransport):
        pass

    transport = Exploding()
    runner = BotRunner(settings=settings, transport=transport, session_factory=session_factory)

    calls = {"count": 0}
    original = runner.handle_message

    def flaky(message: InboundMessage) -> list[str]:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("something unexpected")
        return original(message)

    runner.handle_message = flaky  # type: ignore[method-assign]
    transport.queue_many(["/start", "/start", "/start"])
    handled = runner.poll_once()
    assert calls["count"] == 3, "every message must be attempted"
    assert handled == 2, "the failed one is not counted, the others still are"


def test_a_message_is_acknowledged_only_after_it_has_been_handled(
    settings: Settings, transport: ScriptedTransport, session_factory: sessionmaker[Session]
) -> None:
    """Regression guard: a crash between receiving a message and handling it lost the message.

    The transport used to advance its position as soon as it handed a batch over, so if the
    process died before the batch was handled, those customer messages were gone for good.
    Now the position only moves once a message has been dealt with, so a restart receives it
    again. ``KeyboardInterrupt`` stands in for the process being killed mid-batch.
    """
    runner = BotRunner(settings=settings, transport=transport, session_factory=session_factory)
    transport.queue("/start")

    original = runner.handle_message

    def killed(message: InboundMessage) -> list[str]:
        raise KeyboardInterrupt

    runner.handle_message = killed  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        runner.poll_once()
    assert transport.pending_count == 1, "an unhandled message must stay queued"
    assert transport.sent == []

    runner.handle_message = original  # type: ignore[method-assign]
    assert runner.poll_once() == 1
    assert transport.pending_count == 0
    assert "Welcome" in transport.last_text()


def test_a_message_that_fails_gets_an_apology_instead_of_silence(
    settings: Settings,
    transport: ScriptedTransport,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: one transient database error used to drop the message without a word.

    The failing message is not retried for ever -- that would let one bad message block every
    other customer -- but the customer is told to send it again rather than left waiting.
    """
    runner = BotRunner(settings=settings, transport=transport, session_factory=session_factory)
    original = repository.get_or_create_customer
    calls = {"count": 0}

    def flaky(*args: object, **kwargs: object) -> object:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("database is locked")
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(repository, "get_or_create_customer", flaky)
    transport.queue("/new")
    assert runner.poll_once() == 0
    assert "send it again" in transport.last_text()
    assert transport.pending_count == 0, "a failed message is acknowledged, not replayed for ever"

    transport.queue("/new")
    assert runner.poll_once() == 1
    assert "1. Repair" in transport.last_text()


def test_an_empty_poll_is_not_an_error(bot: BotRunner) -> None:
    assert bot.poll_once() == 0


# --------------------------------------------------------------------- loop control


def test_run_forever_stops_at_max_iterations(bot: BotRunner, transport: ScriptedTransport) -> None:
    transport.queue_many(["/start", "/help"])
    assert bot.run_forever(max_iterations=1, idle_sleep_seconds=0.01) == 2


def test_request_stop_ends_the_loop_immediately(bot: BotRunner) -> None:
    bot.request_stop()
    assert bot.run_forever(idle_sleep_seconds=0.01) == 0


def test_signal_handlers_can_be_installed(bot: BotRunner) -> None:
    bot.install_signal_handlers()
    bot.request_stop()
    assert bot.run_forever(idle_sleep_seconds=0.01) == 0


# --------------------------------------------------------------------- integration


def test_status_change_notification_reaches_the_customer_end_to_end(
    bot: BotRunner,
    worker,
    transport: ScriptedTransport,
    session_factory: sessionmaker[Session],
) -> None:
    """The full loop a buyer cares about: order placed in chat, staff changes status, customer
    is messaged -- once."""
    transport.queue_many(HAPPY_PATH)
    bot.poll_once()

    with session_scope(session_factory) as session:
        order = repository.list_orders(session).items[0]
        reference = order.reference
        repository.change_order_status(
            session, order=order, to_status=OrderStatus.CONFIRMED, actor="amina", note="On our way"
        )

    transport.sent.clear()
    assert worker.process_batch() == (1, 0)
    assert reference in transport.last_text()
    assert "On our way" in transport.last_text()

    transport.sent.clear()
    assert worker.process_batch() == (0, 0)
    assert transport.sent == [], "the customer must not be messaged twice for one change"


# --------------------------------------------------------------------- phone number


def test_the_share_button_reaches_the_transport_and_is_removed_afterwards(
    bot: BotRunner, transport: ScriptedTransport
) -> None:
    transport.queue_many(["/new", "1", "The washing machine will not drain properly"])
    bot.poll_once()
    assert transport.sent[-1].contact_button == "Share my phone number"

    transport.queue("0555123456")
    bot.poll_once()
    assert transport.sent[-1].contact_button is None
    assert transport.sent[-1].remove_keyboard is True


def test_a_shared_own_contact_becomes_a_verified_order_end_to_end(
    bot: BotRunner, transport: ScriptedTransport, session_factory: sessionmaker[Session]
) -> None:
    transport.queue_many(["/new", "1", "The washing machine will not drain properly"])
    transport.queue("213555123456", contact_is_sender=True)
    transport.queue_many(["12 Rue Didouche Mourad, Algiers", "yes"])
    bot.poll_once()

    with session_scope(session_factory) as session:
        order = repository.list_orders(session).items[0]
        assert order.contact_phone == "+213555123456"
        assert order.contact_phone_verified is True


def test_a_made_up_number_never_becomes_an_order(
    bot: BotRunner, transport: ScriptedTransport, session_factory: sessionmaker[Session]
) -> None:
    """The exact conversation that exposed the bug in a manual test."""
    transport.queue_many(
        [
            "/new",
            "1",
            "The washing machine will not drain properly",
            "98765432109876",
            "12 Rue Didouche Mourad, Algiers",
            "yes",
        ]
    )
    bot.poll_once()
    with session_scope(session_factory) as session:
        assert repository.list_orders(session).total == 0


def test_the_runner_uses_the_configured_phone_regions(
    settings: Settings, transport: ScriptedTransport, session_factory: sessionmaker[Session]
) -> None:
    strict = settings.model_copy(update={"phone_allowed_regions": ("DZ",)})
    runner = BotRunner(settings=strict, transport=transport, session_factory=session_factory)
    transport.queue_many(["/new", "1", "The washing machine will not drain properly"])
    transport.queue("+212612345678")
    runner.poll_once()
    assert "DZ (+213)" in transport.last_text()
