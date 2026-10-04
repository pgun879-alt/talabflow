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
    #: Opaque to the application. Set by a transport in manual-acknowledgement mode and handed
    #: back through :meth:`MessageTransport.acknowledge`.
    ack_token: str = ""
    #: ``text`` is the phone number of a contact card the customer shared rather than typed.
    is_contact: bool = False
    #: The shared contact card is the sender's own, as reported by the provider. This is the
    #: only case in which a phone number is known to belong to the account that sent it.
    contact_is_sender: bool = False


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """A message to deliver to a customer."""

    chat_id: str
    text: str
    #: Show a button with this label that shares the customer's own phone number in one tap.
    #: Advisory: a transport with no such control sends the text alone.
    contact_button: str | None = None
    #: Remove that button again.
    remove_keyboard: bool = False


class TransportError(RuntimeError):
    """Delivery or polling failed. The message is safe to log."""


class RateLimitedError(TransportError):
    """The provider is throttling this deployment and said how long to stay quiet.

    Kept apart from an ordinary transient failure because the right reaction is different. The
    message is fine and so is the network: sending anything else before ``retry_after`` has passed
    is refused as well, and usually lengthens the penalty. So callers wait as long as they were
    told to, and do not count the refusal against the message.
    """

    def __init__(self, message: str, *, retry_after: float) -> None:
        super().__init__(message)
        #: Seconds the provider asked for. Zero when it did not say.
        self.retry_after = max(float(retry_after), 0.0)


class PermanentTransportError(TransportError):
    """The message can never be delivered -- the customer blocked the bot, or the chat is gone.

    The outbox worker treats this differently from a transient failure: retrying is pointless,
    so the row goes straight to ``dead`` instead of burning through its attempt budget.
    """


class TransportAuthError(PermanentTransportError):
    """The provider rejected this deployment's credentials.

    This is about the configuration, not about any one message: nothing can be received or sent
    until it is fixed, and no amount of retrying fixes it. A process that hits it stops and says
    so. It must never be handled like an undeliverable message -- that would dead-letter every
    queued notification because of a typo in a token.
    """


class MessageTransport(ABC):
    """A two-way message channel."""

    #: Also the *channel*: customers are recorded with the name of the transport they wrote in
    #: through, and the outbox worker only delivers notifications for its own transport's name.
    name: str = "base"

    #: When false (the default), ``poll`` acknowledges what it returns: the messages are gone from
    #: the provider the moment they are handed over. A consumer that wants to survive a crash sets
    #: this to true and calls :meth:`acknowledge` once each message has actually been handled;
    #: anything not acknowledged is delivered again.
    manual_ack: bool = False

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

    def acknowledge(self, message: InboundMessage) -> None:
        """Mark ``message`` as handled so it is never delivered again.

        Only meaningful when :attr:`manual_ack` is true. Concrete no-op, so a transport with
        nothing to acknowledge needs no override.
        """
        return None

    def close(self) -> None:
        """Release any held resources. Concrete no-op; transports with a client override it."""
        return None
