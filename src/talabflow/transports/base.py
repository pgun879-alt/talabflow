"""The messaging transport seam.

Everything above this layer deals in :class:`InboundMessage` and :class:`OutboundMessage`. That
is what makes the entire conversation flow -- state machine, order creation, notifications --
runnable and testable with **no bot token, no webhook, no public IP and no network**, while the
identical code talks to real Telegram when a token is configured.

This is the single most useful architectural decision in the project. The tutorial version of a
Telegram bot calls the Bot API directly from its message handler, which means its behaviour can
only be verified by messaging it by hand.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from ..models import utcnow


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """A message received from a customer."""

    channel: str
    chat_id: str
    user_id: str
    text: str
    message_id: str = ""
    display_name: str | None = None
    received_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """A message to deliver to a customer."""

    chat_id: str
    text: str


class TransportError(RuntimeError):
    """Delivery or polling failed. The message is safe to log."""


class PermanentTransportError(TransportError):
    """The message can never be delivered -- the customer blocked the bot, or the chat is gone.

    The outbox worker treats this differently from a transient failure: retrying is pointless,
    so the row goes straight to ``dead`` instead of burning through its attempt budget.
    """


class MessageTransport(ABC):
    """A two-way message channel."""

    name: str = "base"

    @abstractmethod
    def poll(self, *, timeout_seconds: float) -> list[InboundMessage]:
        """Return any messages waiting, blocking up to ``timeout_seconds``.

        Returns an empty list when nothing arrived; that is not an error.

        Raises:
            TransportError: on a transport-level failure.
        """

    @abstractmethod
    def send(self, message: OutboundMessage) -> str:
        """Deliver ``message`` and return a provider message id.

        Raises:
            PermanentTransportError: when delivery can never succeed.
            TransportError: on a transient failure worth retrying.
        """

    def close(self) -> None:
        """Release any held resources. Concrete no-op; transports with a client override it."""
        return None
