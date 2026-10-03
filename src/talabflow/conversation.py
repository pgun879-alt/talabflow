"""The order-intake conversation, as an explicit state machine.

Why a state machine and not a chain of ``if`` statements
-------------------------------------------------------
The tutorial version of this bot is one handler full of ``if text == ...``. It breaks the moment
a customer does something unscripted, which they always do: sends ``/new`` halfway through
another order, replies with a service name instead of its number, answers the phone question
with a sentence, restarts the bot mid-flow, or just walks away for a day.

Here every step is a named state, the state is **persisted per customer**, and each step has one
function that either advances or re-prompts. Commands are handled before step logic, so
``/cancel`` and ``/status`` work from anywhere.

The whole machine is pure with respect to messaging: it returns the replies to send rather than
sending them, which is what makes it exhaustively testable without any transport at all.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from sqlalchemy.orm import Session

from . import repository
from .messages import Language, format_service_options, render, status_label
from .models import ConversationState, Customer, Order
from .references import normalise_reference

logger = logging.getLogger(__name__)

MIN_DETAILS_LENGTH: Final = 8
MIN_PHONE_DIGITS: Final = 8
MAX_PHONE_DIGITS: Final = 15
MIN_ADDRESS_LENGTH: Final = 5

#: No menu has anywhere near this many entries. The cap exists because ``int()`` refuses very
#: long digit strings outright (Python's integer-conversion length limit), and a customer who
#: pastes a wall of digits must get a re-prompt, not an exception.
_MAX_MENU_DIGITS: Final = 6

#: Digits, spaces, and the punctuation people put in phone numbers.
_PHONE_ALLOWED: Final = re.compile(r"^[\d\s+()\-.]+$")
_DIGITS: Final = re.compile(r"\d")

#: Affirmatives accepted at the confirmation step, in both languages.
_AFFIRMATIVE: Final[frozenset[str]] = frozenset(
    {"yes", "y", "yep", "yeah", "ok", "okay", "confirm", "نعم", "أجل", "اجل", "موافق", "تم", "ايوه"}
)


class Step(StrEnum):
    """Where the customer is in the intake flow."""

    IDLE = "idle"
    AWAITING_SERVICE = "awaiting_service"
    AWAITING_DETAILS = "awaiting_details"
    AWAITING_PHONE = "awaiting_phone"
    AWAITING_ADDRESS = "awaiting_address"
    CONFIRMING = "confirming"


@dataclass(slots=True)
class Reply:
    """One message the bot wants to send back."""

    text: str


@dataclass(slots=True)
class TurnResult:
    """Everything that came out of handling one inbound message."""

    replies: list[Reply] = field(default_factory=list)
    created_order: Order | None = None

    def add(self, text: str) -> None:
        self.replies.append(Reply(text=text))

    @property
    def texts(self) -> list[str]:
        return [reply.text for reply in self.replies]


def normalise_phone(raw: str) -> str | None:
    """Validate and canonicalise a phone number, or return ``None`` if it is not one.

    Deliberately permissive about *formatting* and strict about *content*: people write
    ``0555 12 34 56`` and ``+213-555-123456``, and both are the same number. Digit count is
    checked against E.164 bounds rather than any country's specific pattern, because a
    country-specific regex is the fastest way to reject a legitimate customer.

    >>> normalise_phone("0555 12 34 56")
    '0555123456'
    >>> normalise_phone("+213 (555) 123-456")
    '+213555123456'
    >>> normalise_phone("٠٥٥٥ ١٢ ٣٤ ٥٦")
    '0555123456'
    >>> normalise_phone("call me maybe")

    Digits are stored as ASCII whatever script they were typed in. The digit pattern matches every
    Unicode decimal digit, so an Arabic-Indic number is accepted -- as it should be -- but storing
    it verbatim would mean staff searching for ``0555`` never find it.
    """
    candidate = raw.strip()
    if not candidate or not _PHONE_ALLOWED.match(candidate):
        return None
    digits = "".join(str(unicodedata.decimal(digit)) for digit in _DIGITS.findall(candidate))
    if not (MIN_PHONE_DIGITS <= len(digits) <= MAX_PHONE_DIGITS):
        return None
    return f"+{digits}" if candidate.startswith("+") else digits


def _resolve_service(text: str, services: tuple[str, ...]) -> str | None:
    """Accept either the menu number or the service name (case-insensitive)."""
    stripped = text.strip()
    # ``isdecimal``, not ``isdigit``: the latter is also true for characters such as "²" and
    # "①", which ``int()`` cannot parse -- the exception would escape and the customer would get
    # no reply. ``isdecimal`` is exactly the set ``int()`` accepts, Arabic-Indic digits included.
    if stripped.isdecimal():
        if len(stripped) > _MAX_MENU_DIGITS:
            return None
        index = int(stripped)
        if 1 <= index <= len(services):
            return services[index - 1]
        return None
    lowered = stripped.casefold()
    return next((name for name in services if name.casefold() == lowered), None)


class ConversationEngine:
    """Drives one customer's intake conversation.

    Holds no per-customer state of its own -- state lives in the database -- so a single engine
    instance serves every customer and survives a restart.
    """

    def __init__(
        self,
        *,
        services: tuple[str, ...],
        business_name: str,
        language: Language = "en",
        max_message_length: int = 1000,
    ) -> None:
        if not services:
            raise ValueError("at least one service type is required")
        self.services = services
        self.business_name = business_name
        self.language = language
        self.max_message_length = max_message_length

    # -- helpers -----------------------------------------------------------------

    def _render(self, key: str, **values: object) -> str:
        return render(key, self.language, **values)

    def _service_menu(self) -> str:
        return format_service_options(self.services)

    # -- entry point -------------------------------------------------------------

    def handle(
        self, session: Session, *, customer: Customer, state: ConversationState, text: str
    ) -> TurnResult:
        """Process one inbound message and return the replies to send.

        Never raises for bad customer input: every invalid value produces a re-prompt, because
        an exception here would mean a silent non-reply to a paying customer.
        """
        result = TurnResult()

        if customer.is_blocked:
            result.add(self._render("blocked"))
            return result

        if len(text) > self.max_message_length:
            result.add(self._render("too_long", limit=self.max_message_length))
            return result

        stripped = text.strip()
        if not stripped:
            result.add(self._render("unknown_command"))
            return result

        # Commands are checked first so they work from any step -- a customer stuck halfway
        # through an order must always be able to escape.
        if stripped.startswith("/"):
            return self._handle_command(session, customer=customer, state=state, text=stripped)

        return self._handle_step(session, customer=customer, state=state, text=stripped)

    # -- commands ----------------------------------------------------------------

    def _handle_command(
        self, session: Session, *, customer: Customer, state: ConversationState, text: str
    ) -> TurnResult:
        result = TurnResult()
        parts = text.split(maxsplit=1)
        # Telegram sends "/start@BotName" in groups; strip the mention.
        command = parts[0].lower().split("@", 1)[0]
        argument = parts[1].strip() if len(parts) > 1 else ""

        if command in {"/start", "/help"}:
            result.add(self._render("welcome", business=self.business_name))
            return result

        if command == "/new":
            state.step = Step.AWAITING_SERVICE.value
            state.reset_draft()
            result.add(self._render("ask_service", options=self._service_menu()))
            return result

        if command == "/cancel":
            if state.step == Step.IDLE.value:
                result.add(self._render("nothing_to_cancel"))
            else:
                state.step = Step.IDLE.value
                state.reset_draft()
                result.add(self._render("cancelled_draft"))
            return result

        if command == "/status":
            return self._handle_status(session, customer=customer, argument=argument)

        result.add(self._render("unknown_command"))
        return result

    def _handle_status(self, session: Session, *, customer: Customer, argument: str) -> TurnResult:
        result = TurnResult()
        if not argument:
            result.add(self._render("status_usage"))
            return result

        reference = normalise_reference(argument)
        if reference is None:
            result.add(self._render("status_not_found"))
            return result

        # Scoped to this customer: a reference alone must not expose someone else's order.
        order = repository.get_customer_order(session, customer_id=customer.id, reference=reference)
        if order is None:
            result.add(self._render("status_not_found"))
            return result

        result.add(
            self._render(
                "status_result",
                reference=order.reference,
                service=order.service_type,
                status=status_label(order.status.value, self.language),
                created=order.created_at.strftime("%Y-%m-%d %H:%M"),
            )
        )
        return result

    # -- steps -------------------------------------------------------------------

    def _handle_step(
        self, session: Session, *, customer: Customer, state: ConversationState, text: str
    ) -> TurnResult:
        result = TurnResult()
        step = state.step

        if step == Step.IDLE.value:
            result.add(self._render("unknown_command"))
            return result

        if step == Step.AWAITING_SERVICE.value:
            service = _resolve_service(text, self.services)
            if service is None:
                result.add(self._render("invalid_service", options=self._service_menu()))
                return result
            state.draft_service_type = service
            state.step = Step.AWAITING_DETAILS.value
            result.add(self._render("ask_details", service=service))
            return result

        if step == Step.AWAITING_DETAILS.value:
            if len(text) < MIN_DETAILS_LENGTH:
                result.add(self._render("details_too_short", minimum=MIN_DETAILS_LENGTH))
                return result
            state.draft_details = text
            state.step = Step.AWAITING_PHONE.value
            result.add(self._render("ask_phone"))
            return result

        if step == Step.AWAITING_PHONE.value:
            phone = normalise_phone(text)
            if phone is None:
                result.add(self._render("invalid_phone"))
                return result
            state.draft_phone = phone
            state.step = Step.AWAITING_ADDRESS.value
            result.add(self._render("ask_address"))
            return result

        if step == Step.AWAITING_ADDRESS.value:
            if len(text) < MIN_ADDRESS_LENGTH:
                result.add(self._render("details_too_short", minimum=MIN_ADDRESS_LENGTH))
                return result
            state.draft_address = text
            state.step = Step.CONFIRMING.value
            result.add(
                self._render(
                    "confirm",
                    service=state.draft_service_type,
                    details=state.draft_details,
                    phone=state.draft_phone,
                    address=state.draft_address,
                )
            )
            return result

        if step == Step.CONFIRMING.value:
            if text.strip().casefold().rstrip("!.") not in _AFFIRMATIVE:
                result.add(self._render("confirm_unclear"))
                return result
            return self._commit_order(session, customer=customer, state=state)

        # An unrecognised persisted step means the schema changed under a live conversation.
        # Resetting is better than looping forever on a state no code handles.
        logger.warning("resetting unknown conversation step %r", step)
        state.step = Step.IDLE.value
        state.reset_draft()
        result.add(self._render("unknown_command"))
        return result

    def _commit_order(
        self, session: Session, *, customer: Customer, state: ConversationState
    ) -> TurnResult:
        result = TurnResult()
        # Defensive: reaching CONFIRMING without every field means a bug upstream, and creating
        # a half-empty order would be worse than restarting the conversation.
        if not all(
            (state.draft_service_type, state.draft_details, state.draft_phone, state.draft_address)
        ):
            logger.error("customer %d reached confirmation with an incomplete draft", customer.id)
            state.step = Step.AWAITING_SERVICE.value
            state.reset_draft()
            result.add(self._render("ask_service", options=self._service_menu()))
            return result

        order = repository.create_order(
            session,
            customer=customer,
            service_type=str(state.draft_service_type),
            details=str(state.draft_details),
            contact_phone=str(state.draft_phone),
            address=str(state.draft_address),
        )
        # Keep the phone on the customer record so a returning customer is recognisable.
        customer.phone = order.contact_phone
        state.step = Step.IDLE.value
        state.reset_draft()

        result.created_order = order
        result.add(
            self._render(
                "order_created",
                reference=order.reference,
                status=status_label(order.status.value, self.language),
            )
        )
        return result
