"""The bot runner: polls a transport, drives the conversation, persists the result.

Each inbound message is one database transaction. If handling a message raises, that message's
work is rolled back and the loop continues with the next one -- one malformed message must never
take the bot down or corrupt a half-written order.

Messages are acknowledged to the transport **after** they have been handled, not when they are
received. If the process dies partway through a batch, the unhandled messages are delivered again
on restart instead of being lost. The cost is at-least-once handling: a crash in the narrow gap
between committing a message's work and acknowledging it means that message is handled twice.

How the loop treats the provider saying no
------------------------------------------
* **Rate limited** -- it waits as long as it was told to. Asking again sooner is refused again and
  tends to lengthen the penalty.
* **Any other poll failure** -- it backs off, doubling up to a minute, so an outage is not met
  with a request every second. The first success resets it.
* **Rejected credentials** -- it stops. A token that is wrong stays wrong, and a loop that keeps
  polling with it looks like a running bot while serving nobody.
"""

from __future__ import annotations

import logging
import signal
import time
from collections.abc import Callable
from types import FrameType
from typing import Final

from sqlalchemy.orm import Session, sessionmaker

from . import repository
from .config import Settings
from .conversation import ConversationEngine, Step
from .db import session_scope
from .messages import render
from .security import SlidingWindowRateLimiter
from .transports.base import (
    InboundMessage,
    MessageTransport,
    OutboundMessage,
    RateLimitedError,
    TransportAuthError,
    TransportError,
)

logger = logging.getLogger(__name__)

#: First pause after a failed poll; doubled on each consecutive failure up to the cap.
POLL_BACKOFF_BASE_SECONDS: Final = 1.0
MAX_POLL_BACKOFF_SECONDS: Final = 60.0

#: Bounds on the pause taken when a poll is rate limited: at least a second, so a provider that
#: names no delay is not asked again at once; at most an hour, so a nonsense value cannot stop
#: the bot for a day.
MIN_RATE_LIMIT_WAIT_SECONDS: Final = 1.0
MAX_RATE_LIMIT_WAIT_SECONDS: Final = 3600.0

#: The longest a single conversational reply waits out a rate limit before being dropped. Replies
#: are sent from the poll loop, so waiting longer for one customer would stall all the others.
MAX_REPLY_WAIT_SECONDS: Final = 5.0

#: Sleeps are taken in slices no longer than this, so a stop request is noticed promptly.
_SLEEP_SLICE_SECONDS: Final = 1.0

#: How many flood-warning records are kept before the expired ones are swept out.
_FLOOD_RECORDS_BEFORE_PRUNE: Final = 1024


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
        #: identity -> the moment until which that customer has already been told to slow down.
        self._flood_warned_until: dict[str, float] = {}
        self._stopping = False
        self._poll_failures = 0
        #: How long to wait before polling again after a failure; zero after a clean poll.
        self._retry_delay = 0.0
        # Seams for tests: time is read and spent only through these two.
        self._clock: Callable[[], float] = time.monotonic
        self._sleep: Callable[[float], None] = time.sleep
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
        now = self._clock()
        allowed, retry_after = self.flood_limiter.check(identity, now=now)
        if not allowed:
            # One warning per window, then silence. Answering every excess message doubles the
            # traffic of the very flood this is meant to stop, and spends the bot's own send
            # quota -- the one its real customers depend on -- on someone who is not listening.
            if self._flood_warned_until.get(identity, 0.0) > now:
                return []
            self._remember_flood_warning(identity, now)
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

    def _remember_flood_warning(self, identity: str, now: float) -> None:
        """Record that ``identity`` was warned, for one full window from now."""
        if len(self._flood_warned_until) >= _FLOOD_RECORDS_BEFORE_PRUNE:
            self._flood_warned_until = {
                key: until for key, until in self._flood_warned_until.items() if until > now
            }
        self._flood_warned_until[identity] = now + self.flood_limiter.window_seconds

    def _pause(self, seconds: float) -> None:
        """Sleep for ``seconds``, in short slices so a stop request cuts it short."""
        remaining = seconds
        while remaining > 0 and not self._stopping:
            step = min(remaining, _SLEEP_SLICE_SECONDS)
            self._sleep(step)
            remaining -= step

    def _send(
        self,
        chat_id: str,
        text: str,
        *,
        contact_button: str | None = None,
        remove_keyboard: bool = False,
    ) -> None:
        outbound = OutboundMessage(
            chat_id=chat_id,
            text=text,
            contact_button=contact_button,
            remove_keyboard=remove_keyboard,
        )
        try:
            try:
                self.transport.send(outbound)
            except RateLimitedError as exc:
                # A short pause is worth taking: the customer is waiting for this answer. A long
                # one is not -- it would hold up every other customer -- and one retry is the
                # limit, so a provider that keeps refusing cannot pin the loop here.
                if exc.retry_after > MAX_REPLY_WAIT_SECONDS:
                    raise
                self._pause(max(exc.retry_after, MIN_RATE_LIMIT_WAIT_SECONDS))
                self.transport.send(outbound)
        except TransportError as exc:
            # A failed conversational reply is not worth queueing: by the time it were retried
            # the customer's context would be gone. Status notifications, which *do* matter
            # later, go through the durable outbox instead.
            logger.warning("could not deliver a reply to %s: %s", chat_id, exc)

    # -- loop --------------------------------------------------------------------

    def poll_once(self) -> int:
        """Poll once and handle everything received. Returns the number of messages handled.

        Raises:
            TransportAuthError: when the provider rejects the bot's credentials. The loop is
                marked as stopping first; see the module docstring.
        """
        try:
            messages = self.transport.poll(
                timeout_seconds=self.settings.telegram_poll_timeout_seconds
            )
        except TransportAuthError:
            self._stopping = True
            logger.error("the messaging provider rejected the bot's credentials; stopping")
            raise
        except RateLimitedError as exc:
            self._poll_failures += 1
            self._retry_delay = min(
                max(exc.retry_after, MIN_RATE_LIMIT_WAIT_SECONDS), MAX_RATE_LIMIT_WAIT_SECONDS
            )
            logger.warning("poll rate limited; waiting %.0fs", self._retry_delay)
            return 0
        except TransportError as exc:
            self._poll_failures += 1
            # The exponent is bounded so a week-long outage does not compute 2**600000.
            doublings = min(self._poll_failures - 1, 16)
            self._retry_delay = min(
                POLL_BACKOFF_BASE_SECONDS * 2**doublings, MAX_POLL_BACKOFF_SECONDS
            )
            logger.warning("poll failed: %s (next try in %.0fs)", exc, self._retry_delay)
            return 0
        self._poll_failures = 0
        self._retry_delay = 0.0

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

        Raises:
            TransportAuthError: when the provider rejects the bot's credentials.
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
            if self._retry_delay > 0:
                self._pause(self._retry_delay)
            elif handled == 0:
                self._pause(idle_sleep_seconds)
        logger.info("bot stopped", extra={"handled": total})
        return total
