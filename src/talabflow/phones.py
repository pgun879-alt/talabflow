"""Phone-number validation, per country.

Why a library and not a regular expression
------------------------------------------
The first version of this bot accepted any 8 to 15 digits. That is the E.164 length range and
nothing more, so ``98765432109876`` -- fourteen digits belonging to no country at all -- was taken
as a contact number. Counting digits cannot tell a phone number from a row of keys.

Real numbers are validated against the numbering plan of *their own country*: how long a number
is, and which prefixes exist, differs for every one of them. Those rules also change as regulators
open new ranges. Writing them by hand for one country is a maintenance burden; for every country
it is not credible. ``phonenumbers`` is the Python port of Google's libphonenumber and carries the
plans for all of them, so this module is a thin, strict wrapper around it.

How a number is read
--------------------
* A number that starts with ``+`` (or ``00``) names its own country and is validated against
  that country's plan, wherever the business is.
* A number typed the local way -- ``0555 12 34 56`` -- has no country in it, so it is read as a
  number of the configured *default region*. With no default region it is refused with a request
  to include the country code: guessing a country would store a confidently wrong number.
* Every accepted number is stored in E.164 (``+213555123456``): one canonical form to search,
  export and dial.

What this does **not** prove: that the number is in service, or that it belongs to the person who
typed it. A syntactically valid number can still be someone else's. Ownership is only established
when the customer shares their own contact through the messaging app -- see
:attr:`talabflow.transports.base.InboundMessage.contact_is_sender`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat, PhoneNumberType

#: Digits, spaces, and the punctuation people put in phone numbers, with at most one ``+`` and
#: only at the start. Checked *before* parsing, because libphonenumber is forgiving by design: it
#: reads letters as a keypad spelling ("0555abc456" -> 0555222456) and drops a stray plus sign, so
#: it would happily turn a typo into a valid-looking number.
_PHONE_ALLOWED: Final = re.compile(r"\+?[\d\s()\-.]+")
_DIGIT: Final = re.compile(r"\d")

#: Line types a customer can actually be called back on. Premium-rate, toll-free, shared-cost,
#: pager and voicemail numbers are well-formed but are not somebody's contact number.
_CALLABLE_TYPES: Final = frozenset(
    {
        PhoneNumberType.MOBILE,
        PhoneNumberType.FIXED_LINE,
        PhoneNumberType.FIXED_LINE_OR_MOBILE,
        PhoneNumberType.VOIP,
    }
)

#: Used only to show an example number when no region is configured at all.
_FALLBACK_EXAMPLE_REGION: Final = "DZ"


class PhoneProblem(StrEnum):
    """Why a phone number was refused. Each one maps to its own message to the customer."""

    NOT_A_NUMBER = "not_a_number"
    COUNTRY_CODE_REQUIRED = "country_code_required"
    REGION_NOT_ALLOWED = "region_not_allowed"


class PhoneError(ValueError):
    """Raised by :func:`parse_phone` for a number that cannot be accepted."""

    def __init__(self, problem: PhoneProblem) -> None:
        super().__init__(problem.value)
        self.problem = problem


@dataclass(frozen=True, slots=True)
class Phone:
    """A validated phone number."""

    #: Canonical form, e.g. ``+213555123456``. This is what is stored.
    e164: str
    #: ISO 3166 two-letter code of the country the number belongs to, e.g. ``DZ``.
    region: str
    #: Grouped for reading back to a person, e.g. ``+213 555 12 34 56``.
    international: str


def is_supported_region(code: str) -> bool:
    """True when ``code`` is a two-letter country code the numbering data knows about."""
    return code in phonenumbers.SUPPORTED_REGIONS


def _ascii_digits(text: str) -> str:
    """Replace every Unicode decimal digit with its ASCII equivalent.

    Arabic-Indic (``٠٥٥٥``) and Persian (``۰۵۵۵``) digits are what many customers' keyboards
    produce. They are legitimate input and must not be stored verbatim, or a search for ``0555``
    would never match.
    """
    return _DIGIT.sub(lambda match: str(unicodedata.decimal(match.group())), text)


def _without_invisible_marks(text: str) -> str:
    """Drop Unicode format characters: direction marks, embeddings, zero-width joiners.

    A number copied out of a contacts app or a right-to-left message routinely arrives wrapped in
    them (``\u202a+213 555 12 34 56\u202c``). The customer cannot see them, so refusing the number
    for them would be refusing a correct number with no way for the customer to fix it.
    """
    return "".join(char for char in text if unicodedata.category(char) != "Cf")


def parse_phone(
    raw: str,
    *,
    default_region: str = "",
    allowed_regions: frozenset[str] = frozenset(),
    international: bool = False,
) -> Phone:
    """Validate ``raw`` and return it in canonical form.

    Args:
        raw: What the customer sent.
        default_region: Country used to read a number that carries no country code. Empty means
            a country code is mandatory.
        allowed_regions: When non-empty, only numbers from these countries are accepted.
        international: The number is known to be in international form even without a leading
            ``+``. Telegram delivers a shared contact that way (``213555123456``).

    Raises:
        PhoneError: with the specific :class:`PhoneProblem`.

    >>> parse_phone("0555 12 34 56", default_region="DZ").e164
    '+213555123456'
    >>> parse_phone("+212 612-345678", default_region="DZ").region
    'MA'
    >>> parse_phone("٠٥٥٥١٢٣٤٥٦", default_region="DZ").international
    '+213 555 12 34 56'
    """
    candidate = _without_invisible_marks(raw).strip()
    if not candidate or not _PHONE_ALLOWED.fullmatch(candidate):
        raise PhoneError(PhoneProblem.NOT_A_NUMBER)
    candidate = _ascii_digits(candidate)

    if candidate.startswith("00"):
        # The international dialling prefix in most of the world. Normalised here so it works
        # whatever the default region's own prefix is, and when there is no default region.
        candidate = "+" + candidate[2:]
    elif international and not candidate.startswith("+"):
        candidate = "+" + candidate

    if not candidate.startswith("+") and not default_region:
        raise PhoneError(PhoneProblem.COUNTRY_CODE_REQUIRED)

    try:
        number = phonenumbers.parse(candidate, default_region or None)
    except NumberParseException as exc:
        raise PhoneError(PhoneProblem.NOT_A_NUMBER) from exc

    # ``is_valid_number`` checks length *and* prefix against the country's numbering plan.
    # ``is_possible_number`` would only check the length, which is how a made-up number passes.
    if not phonenumbers.is_valid_number(number):
        raise PhoneError(PhoneProblem.NOT_A_NUMBER)
    if phonenumbers.number_type(number) not in _CALLABLE_TYPES:
        raise PhoneError(PhoneProblem.NOT_A_NUMBER)

    region = phonenumbers.region_code_for_number(number)
    if region is None:
        raise PhoneError(PhoneProblem.NOT_A_NUMBER)
    if allowed_regions and region not in allowed_regions:
        raise PhoneError(PhoneProblem.REGION_NOT_ALLOWED)

    return Phone(
        e164=phonenumbers.format_number(number, PhoneNumberFormat.E164),
        region=region,
        international=phonenumbers.format_number(number, PhoneNumberFormat.INTERNATIONAL),
    )


def format_international(e164: str) -> str:
    """Group a stored E.164 number for reading: ``+213555123456`` -> ``+213 555 12 34 56``.

    Returns the input unchanged if it cannot be parsed, so a row written before validation was
    introduced still displays.
    """
    try:
        number = phonenumbers.parse(e164, None)
    except NumberParseException:
        return e164
    return phonenumbers.format_number(number, PhoneNumberFormat.INTERNATIONAL)


def example_numbers(region: str = "") -> tuple[str, str]:
    """An example mobile number for ``region`` as ``(local form, international form)``.

    Shown to a customer whose number was refused, because "invalid number" on its own does not
    tell anyone what a valid one looks like.
    """
    code = region if is_supported_region(region) else _FALLBACK_EXAMPLE_REGION
    example = phonenumbers.example_number_for_type(
        code, PhoneNumberType.MOBILE
    ) or phonenumbers.example_number(code)
    if example is None:  # pragma: no cover - every supported region ships an example
        return "", ""
    return (
        phonenumbers.format_number(example, PhoneNumberFormat.NATIONAL),
        phonenumbers.format_number(example, PhoneNumberFormat.INTERNATIONAL),
    )


def describe_regions(regions: frozenset[str] | tuple[str, ...]) -> str:
    """Render regions for a customer: ``DZ (+213), MA (+212)``."""
    return ", ".join(
        f"{code} (+{phonenumbers.country_code_for_region(code)})" for code in sorted(regions)
    )
