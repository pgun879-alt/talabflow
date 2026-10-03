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
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from sqlalchemy.orm import Session

from . import repository
from .messages import Language, format_service_options, render, status_label
from .models import ConversationState, Customer, Order
from .phones import (
    Phone,
    PhoneError,
    PhoneProblem,
    describe_regions,
    example_numbers,
    format_international,
    parse_phone,
)
from .references import normalise_reference

logger = logging.getLogger(__name__)

MIN_DETAILS_LENGTH: Final = 8
MIN_ADDRESS_LENGTH: Final = 5

#: No menu has anywhere near this many entries. The cap exists because ``int()`` refuses very
#: long digit strings outright (Python's integer-conversion length limit), and a customer who
#: pastes a wall of digits must get a re-prompt, not an exception.
_MAX_MENU_DIGITS: Final = 6

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
    #: Label of a one-tap "share my phone number" button to show under this message.
    #: A transport that has no such button ignores it; typing the number always works.
    contact_button: str | None = None
    #: Take that button away again once the phone step is over.
    remove_keyboard: bool = False


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
        phone_default_region: str = "",
        phone_allowed_regions: tuple[str, ...] = (),
    ) -> None:
        if not services:
            raise ValueError("at least one service type is required")
        self.services = services
        self.business_name = business_name
        self.language = language
        self.max_message_length = max_message_length
        self.phone_default_region = phone_default_region
        self.phone_allowed_regions = frozenset(phone_allowed_regions)
        # The country whose numbers are shown as examples: the default one, else the
        # first allowed one, else whatever the phones module falls back to.
        self._example_region = phone_default_region or next(
            iter(sorted(self.phone_allowed_regions)), ""
        )

    # -- helpers -----------------------------------------------------------------

    def _render(self, key: str, **values: object) -> str:
        return render(key, self.language, **values)

    def _service_menu(self) -> str:
        return format_service_options(self.services)

    # -- entry point -------------------------------------------------------------

    def handle(
        self,
        session: Session,
        *,
        customer: Customer,
        state: ConversationState,
        text: str,
        shared_contact: bool = False,
        contact_is_own: bool = False,
    ) -> TurnResult:
        """Process one inbound message and return the replies to send.

        Never raises for bad customer input: every invalid value produces a re-prompt, because
        an exception here would mean a silent non-reply to a paying customer.

        Args:
            shared_contact: ``text`` is the phone number of a contact card the customer
                shared, not something they typed.
            contact_is_own: that contact card is the customer's own, as reported by the
                messaging app. Only then is the number recorded as verified.
        """
        step_before = state.step
        result = self._dispatch(
            session,
            customer=customer,
            state=state,
            text=text,
            shared_contact=shared_contact,
            contact_is_own=shared_contact and contact_is_own,
        )
        if result.replies and not customer.is_blocked:
            if state.step == Step.AWAITING_PHONE.value:
                # Offered on every prompt of the phone step, re-prompts included: a customer
                # whose typed number was refused is exactly who needs the one-tap way.
                result.replies[-1].contact_button = self._render("share_phone_button")
            elif step_before == Step.AWAITING_PHONE.value:
                result.replies[0].remove_keyboard = True
        return result

    def _dispatch(
        self,
        session: Session,
        *,
        customer: Customer,
        state: ConversationState,
        text: str,
        shared_contact: bool,
        contact_is_own: bool,
    ) -> TurnResult:
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

        return self._handle_step(
            session,
            customer=customer,
            state=state,
            text=stripped,
            shared_contact=shared_contact,
            contact_is_own=contact_is_own,
        )

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

    def _read_phone(self, text: str, *, shared_contact: bool, contact_is_own: bool) -> Phone:
        """Validate what was sent at the phone step.

        A typed number is read as typed. A shared contact card needs more care, because the
        messaging app delivers the customer's *own* number in international form with no
        leading ``+`` (``213555123456``): read the local way that would be refused, or worse,
        matched to the wrong country. A card for somebody else carries the number however it
        was saved in the address book, so it is read as typed first and as international
        second.

        Raises:
            PhoneError: with the problem from the first reading, which is the one that
                describes what the customer actually sent.
        """
        readings: tuple[bool, ...]
        if contact_is_own:
            readings = (True,)
        elif shared_contact:
            readings = (False, True)
        else:
            readings = (False,)

        first_error: PhoneError | None = None
        for international in readings:
            try:
                return parse_phone(
                    text,
                    default_region=self.phone_default_region,
                    allowed_regions=self.phone_allowed_regions,
                    international=international,
                )
            except PhoneError as exc:
                first_error = first_error or exc
        assert first_error is not None  # ``readings`` is never empty
        raise first_error

    def _phone_refusal(self, problem: PhoneProblem) -> str:
        """The message for a refused number. Each one says what to send instead."""
        local, international = example_numbers(self._example_region)
        if problem is PhoneProblem.COUNTRY_CODE_REQUIRED:
            return self._render("phone_needs_country_code", international=international)
        if problem is PhoneProblem.REGION_NOT_ALLOWED:
            return self._render(
                "phone_region_not_allowed", regions=describe_regions(self.phone_allowed_regions)
            )
        if not self.phone_default_region:
            # With no default country a local-style example would itself be refused.
            return self._render("phone_needs_country_code", international=international)
        return self._render("invalid_phone", local=local, international=international)

    def _handle_step(
        self,
        session: Session,
        *,
        customer: Customer,
        state: ConversationState,
        text: str,
        shared_contact: bool = False,
        contact_is_own: bool = False,
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
            try:
                phone = self._read_phone(
                    text, shared_contact=shared_contact, contact_is_own=contact_is_own
                )
            except PhoneError as exc:
                result.add(self._phone_refusal(exc.problem))
                return result
            state.draft_phone = phone.e164
            state.draft_phone_verified = contact_is_own
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
                    # Read back grouped, so a mistyped digit is easy to spot before saying yes.
                    phone=format_international(state.draft_phone or ""),
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
            contact_phone_verified=bool(state.draft_phone_verified),
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
