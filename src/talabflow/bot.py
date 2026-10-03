"""The bot runner: polls a transport, drives the conversation, persists the result.

Each inbound message is one database transaction. If handling a message raises, that message's
work is rolled back and the loop continues with the next one -- one malformed message must never
take the bot down or corrupt a half-written order.

Messages are acknowledged to the transport **after** they have been handled, not when they are
received. If the process dies partway through a batch, the unhandled messages are delivered again
on restart instead of being lost. The cost is at-least-once handling: a crash in the narrow gap
between committing a message's work and acknowledging it means that message is handled twice.
"""

from __future__ import annotations

import logging
import signal
import time
from types import FrameType

from sqlalchemy.orm import Session, sessionmaker

from . import repository
from .config import Settings
from .conversation import ConversationEngine, Step
from .db import session_scope
from .messages import render
from .security import SlidingWindowRateLimiter
from .transports.base import InboundMessage, MessageTransport, OutboundMessage, TransportError

logger = logging.getLogger(__name__)


class BotRunner:
    """Wires a transport to the conversation engine and the database."""

    def __init__(
        self,
        *,
        settings: Settings,
        transport: MessageTransport,
        session_factory: sessionmaker[Session],
        engine: ConversationEngine | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.session_factory = session_factory
        self.engine = engine or ConversationEngine(
            services=settings.service_types,
            business_name=settings.business_name,
            language=settings.default_language,
            max_message_length=settings.max_message_length,
            phone_default_region=settings.phone_default_region,
            phone_allowed_regions=settings.phone_allowed_regions,
        )
        self.flood_limiter = SlidingWindowRateLimiter(limit=settings.user_messages_per_minute)
        self._stopping = False
        # This runner acknowledges each message itself, once it has been handled.
        self.transport.manual_ack = True

    # -- lifecycle ---------------------------------------------------------------

    def request_stop(self) -> None:
        """Ask the loop to finish the current batch and exit."""
        self._stopping = True

    def install_signal_handlers(self) -> None:
        """Stop cleanly on SIGINT/SIGTERM instead of dying mid-transaction."""

        def handler(signum: int, _frame: FrameType | None) -> None:
            logger.info("received signal %d; shutting down after the current batch", signum)
            self.request_stop()

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    # -- message handling --------------------------------------------------------

    def handle_message(self, message: InboundMessage) -> list[str]:
        """Handle one message in its own transaction and return the replies that were sent."""
        identity = f"{message.channel}:{message.user_id}"
        allowed, retry_after = self.flood_limiter.check(identity)
        if not allowed:
            logger.warning(
                "flood limit hit",
                extra={"identity": identity, "retry_after": round(retry_after, 1)},
            )
            text = render("rate_limited", self.settings.default_language)
            self._send(message.chat_id, text)
            return [text]

        with session_scope(self.session_factory) as session:
            customer = repository.get_or_create_customer(
                session,
                channel=message.channel,
                channel_user_id=message.user_id,
                chat_id=message.chat_id,
                display_name=message.display_name,
            )
            state = repository.get_conversation_state(
                session, customer, default_step=Step.IDLE.value
            )
            result = self.engine.handle(
                session,
                customer=customer,
                state=state,
                text=message.text,
                shared_contact=message.is_contact,
                contact_is_own=message.contact_is_sender,
            )
            # Copied out while the session is open; the replies are sent after the commit.
            replies = list(result.replies)
            texts = result.texts
            reference = result.created_order.reference if result.created_order else None

        # Sending happens *after* the commit on purpose. Telling a customer their order number
        # before the transaction commits would be a lie if the commit then failed.
        for reply in replies:
            self._send(
                message.chat_id,
                reply.text,
                contact_button=reply.contact_button,
                remove_keyboard=reply.remove_keyboard,
            )
        if reference:
            logger.info("order created", extra={"reference": reference, "channel": message.channel})
        return texts

    def _send(
        self,
        chat_id: str,
        text: str,
        *,
        contact_button: str | None = None,
        remove_keyboard: bool = False,
    ) -> None:
        try:
            self.transport.send(
                OutboundMessage(
                    chat_id=chat_id,
                    text=text,
                    contact_button=contact_button,
                    remove_keyboard=remove_keyboard,
                )
            )
        except TransportError as exc:
            # A failed conversational reply is not worth queueing: by the time it were retried
            # the customer's context would be gone. Status notifications, which *do* matter
            # later, go through the durable outbox instead.
            logger.warning("could not deliver a reply to %s: %s", chat_id, exc)

    # -- loop --------------------------------------------------------------------

    def poll_once(self) -> int:
        """Poll once and handle everything received. Returns the number of messages handled."""
        try:
            messages = self.transport.poll(
                timeout_seconds=self.settings.telegram_poll_timeout_seconds
            )
        except TransportError as exc:
            logger.warning("poll failed: %s", exc)
            return 0

        handled = 0
        for message in messages:
            try:
                self.handle_message(message)
                handled += 1
            except Exception:
                # Deliberately broad: the loop must survive any single bad message. The
                # transaction for that message was already rolled back by session_scope.
                logger.exception(
                    "failed to handle a message",
                    extra={"channel": message.channel, "user": message.user_id},
                )
                self._apologise(message)
            # Acknowledged only now, handled or not. Acknowledging on receipt would lose every
            # message still waiting in this batch if the process died here. A message that
            # *failed* is acknowledged too: replaying it for ever would let one bad message block
            # every other customer, so the customer is asked to send it again instead.
            # ``KeyboardInterrupt`` and ``SystemExit`` are not ``Exception``, so a shutdown
            # mid-message skips this line and the message is delivered again on restart.
            self.transport.acknowledge(message)
        return handled

    def _apologise(self, message: InboundMessage) -> None:
        """Tell the customer their message was not processed, rather than saying nothing."""
        try:
            self._send(message.chat_id, render("try_again", self.settings.default_language))
        except Exception:
            # The apology is best-effort; failing to send it must not break the loop either.
            logger.exception("could not send the apology", extra={"user": message.user_id})

    def run_forever(
        self, *, idle_sleep_seconds: float = 1.0, max_iterations: int | None = None
    ) -> int:
        """Poll until stopped.

        Args:
            idle_sleep_seconds: Pause after an empty poll, so a non-blocking transport does not
                spin the CPU.
            max_iterations: Stop after this many polls. Used by tests and the demo; ``None``
                means run until signalled.

        Returns:
            Total messages handled.
        """
        total = 0
        iterations = 0
        logger.info(
            "bot started",
            extra={"transport": self.transport.name, "services": len(self.engine.services)},
        )
        while not self._stopping:
            if max_iterations is not None and iterations >= max_iterations:
                break
            iterations += 1
            handled = self.poll_once()
            total += handled
            if handled == 0 and not self._stopping:
                time.sleep(idle_sleep_seconds)
        logger.info("bot stopped", extra={"handled": total})
        return total
