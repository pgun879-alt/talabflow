"""An in-memory transport for tests and the offline demo.

Inbound messages are queued up front; outbound messages are recorded so a test (or the demo
script) can assert on exactly what the customer would have received.

It can also be told to fail on demand, which is how the outbox worker's retry, backoff and
dead-lettering behaviour is tested without waiting on a real network.
"""

from __future__ import annotations

import contextlib
import itertools
from collections import deque

from .base import (
    InboundMessage,
    MessageTransport,
    OutboundMessage,
    PermanentTransportError,
    TransportError,
)

DEFAULT_CHANNEL = "scripted"


class ScriptedTransport(MessageTransport):
    """A transport driven by a pre-loaded script of inbound messages."""

    name = DEFAULT_CHANNEL

    def __init__(self, *, channel: str = DEFAULT_CHANNEL) -> None:
        self.channel = channel
        self.name = channel
        self._inbound: deque[InboundMessage] = deque()
        self.sent: list[OutboundMessage] = []
        #: Number of remaining sends that should raise a transient error.
        self.fail_next_sends = 0
        #: When true, every send raises :class:`PermanentTransportError`.
        self.permanent_failure = False
        self._ids = itertools.count(1)

    # -- script construction -----------------------------------------------------

    def queue(
        self,
        text: str,
        *,
        user_id: str = "1001",
        chat_id: str | None = None,
        display_name: str | None = "Demo Customer",
    ) -> InboundMessage:
        """Append one inbound message to the script."""
        message = InboundMessage(
            channel=self.channel,
            chat_id=chat_id or user_id,
            user_id=user_id,
            text=text,
            message_id=str(next(self._ids)),
            display_name=display_name,
        )
        self._inbound.append(message)
        return message

    def queue_many(self, texts: list[str], *, user_id: str = "1001") -> None:
        for text in texts:
            self.queue(text, user_id=user_id)

    # -- transport interface -----------------------------------------------------

    def poll(self, *, timeout_seconds: float) -> list[InboundMessage]:
        """Return every queued message at once. Never blocks -- this is an offline transport.

        In manual-acknowledgement mode the messages stay queued until :meth:`acknowledge` is
        called, so an unhandled message is returned again by the next poll -- the same contract
        the Telegram transport honours, which is what lets crash recovery be tested offline.
        """
        if self.manual_ack:
            return list(self._inbound)
        drained = list(self._inbound)
        self._inbound.clear()
        return drained

    def acknowledge(self, message: InboundMessage) -> None:
        # Already acknowledged, or never queued here: nothing to do either way.
        with contextlib.suppress(ValueError):
            self._inbound.remove(message)

    def send(self, message: OutboundMessage) -> str:
        if self.permanent_failure:
            raise PermanentTransportError("scripted permanent failure")
        if self.fail_next_sends > 0:
            self.fail_next_sends -= 1
            raise TransportError("scripted transient failure")
        self.sent.append(message)
        return f"scripted-{len(self.sent)}"

    # -- assertions helpers ------------------------------------------------------

    @property
    def pending_count(self) -> int:
        return len(self._inbound)

    def sent_texts(self) -> list[str]:
        return [message.text for message in self.sent]

    def last_text(self) -> str:
        if not self.sent:
            raise AssertionError("no message was sent")
        return self.sent[-1].text

    def find_sent(self, needle: str) -> OutboundMessage | None:
        """Return the first sent message containing ``needle``, if any."""
        return next((message for message in self.sent if needle in message.text), None)
