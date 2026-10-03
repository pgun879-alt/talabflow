"""Tests for phone-number validation.

The bug these exist for: the bot once accepted ``98765432109876`` -- fourteen digits that belong to
no country -- because it only counted digits. A contact number nobody can call is a lost order, so
this module is tested against what real numbering plans allow, not against a length range.
"""

from __future__ import annotations

import pytest

from talabflow.phones import (
    PhoneError,
    PhoneProblem,
    describe_regions,
    example_numbers,
    format_international,
    is_supported_region,
    parse_phone,
)


def _problem(raw: str, **kwargs: object) -> PhoneProblem:
    with pytest.raises(PhoneError) as caught:
        parse_phone(raw, **kwargs)  # type: ignore[arg-type]
    return caught.value.problem


# --------------------------------------------------------------------- accepted


@pytest.mark.parametrize(
    "raw",
    [
        "0555123456",
        "0555 12 34 56",
        "0555-12-34-56",
        "0555.12.34.56",
        "  0555123456  ",
        "(0555) 12 34 56",
        "+213 (555) 123-456",
        "+213555123456",
        "00213555123456",  # the international dialling prefix instead of "+"
        "+213 0555 12 34 56",  # the trunk zero kept after the country code: a very common slip
        "213555123456",  # the country code without the plus
        # Arabic-Indic and Persian digits are what many customers' keyboards produce.
        "٠٥٥٥١٢٣٤٥٦",
        "+٢١٣ ٥٥٥ ١٢٣ ٤٥٦",
        "۰۵۵۵۱۲۳۴۵۶",
        "０５５５１２３４５６",  # full-width digits, from an East Asian input method
        # Invisible direction marks: what copying a number out of a contacts app or a
        # right-to-left message leaves around it. The customer cannot see or remove them.
        "\u202a+213 555 12 34 56\u202c",
        "\u200f0555123456",
        "\u2066+213555123456\u2069",
        "+213\u00a0555\u00a012\u00a034\u00a056",  # non-breaking spaces
    ],
)
def test_every_way_of_writing_one_number_gives_the_same_stored_value(raw: str) -> None:
    """People write numbers many ways; rejecting formatting rejects paying customers. Storing
    each spelling verbatim would mean a search for one never finds the others."""
    phone = parse_phone(raw, default_region="DZ")
    assert phone.e164 == "+213555123456"
    assert phone.region == "DZ"
    assert phone.international == "+213 555 12 34 56"


@pytest.mark.parametrize(
    ("raw", "e164"),
    [
        ("0770 12 34 56", "+213770123456"),  # mobile
        ("0660123456", "+213660123456"),  # mobile
        ("021 43 21 00", "+21321432100"),  # an Algiers landline: one digit shorter than a mobile
    ],
)
def test_mobiles_and_landlines_of_the_default_country_are_accepted(raw: str, e164: str) -> None:
    assert parse_phone(raw, default_region="DZ").e164 == e164


@pytest.mark.parametrize(
    ("raw", "region", "e164"),
    [
        ("+212 612-345678", "MA", "+212612345678"),
        ("00212612345678", "MA", "+212612345678"),
        ("+966 50 123 4567", "SA", "+966501234567"),
        ("+33 6 12 34 56 78", "FR", "+33612345678"),
        ("+44 7400 123456", "GB", "+447400123456"),
        ("+1 202 555 0123", "US", "+12025550123"),
    ],
)
def test_a_number_with_a_country_code_is_validated_against_its_own_country(
    raw: str, region: str, e164: str
) -> None:
    """The country code decides the rules, wherever the business is: a Moroccan number has
    Moroccan lengths and prefixes even when the default country is Algeria."""
    phone = parse_phone(raw, default_region="DZ")
    assert (phone.region, phone.e164) == (region, e164)


def test_the_country_code_wins_over_the_default_region() -> None:
    assert parse_phone("+213555123456", default_region="SA").region == "DZ"


def test_the_same_local_digits_mean_a_different_number_in_a_different_country() -> None:
    """Why the default region matters: a local number has no country in it, and the very same
    digits are a valid number in two countries at once."""
    assert parse_phone("0612345678", default_region="MA").e164 == "+212612345678"
    assert parse_phone("0612345678", default_region="DZ").e164 == "+213612345678"


# --------------------------------------------------------------------- refused


def test_the_reported_number_is_refused() -> None:
    """Fourteen digits starting with 98 -- accepted by the old digit-count check."""
    assert _problem("98765432109876", default_region="DZ") is PhoneProblem.NOT_A_NUMBER
    # Even read as an Iranian number (+98) it has the wrong length for Iran.
    assert _problem("+98765432109876", default_region="DZ") is PhoneProblem.NOT_A_NUMBER
    assert _problem("98765432109876", international=True) is PhoneProblem.NOT_A_NUMBER


@pytest.mark.parametrize(
    "raw",
    [
        "0555123",  # too short for the country
        "05551234567",  # one digit too many
        "0155123456",  # right length, a prefix that does not exist
        "0000000000",
        "123",
        "1234567890123456789",
        "+999123456789",  # no such country code
        "+800 1234 5678",  # international freephone: not somebody's contact number
        "17",  # a short code
        "+",
        "",
        "   ",
        "\u200f\u200e",  # nothing but invisible marks
        "0555²123456",  # a superscript is not a digit
        "call me maybe",
        "0555abc456",  # letters must not be read as a keypad spelling
        "0555 12 34 56 ext 9",
        "0555+123456",  # a plus sign anywhere but the start
        "++213555123456",
        "<script>alert(1)</script>",
        "=0555123456",
    ],
)
def test_things_that_are_not_a_callable_number_are_refused(raw: str) -> None:
    assert _problem(raw, default_region="DZ") is PhoneProblem.NOT_A_NUMBER


def test_a_very_long_digit_string_is_refused_not_an_exception() -> None:
    assert _problem("9" * 5000, default_region="DZ") is PhoneProblem.NOT_A_NUMBER


# --------------------------------------------------------------------- no default region


def test_without_a_default_region_a_local_number_needs_its_country_code() -> None:
    """Guessing a country would store a confidently wrong number, so it asks instead."""
    assert _problem("0555123456") is PhoneProblem.COUNTRY_CODE_REQUIRED


@pytest.mark.parametrize("raw", ["+213555123456", "00213555123456"])
def test_without_a_default_region_an_international_number_still_works(raw: str) -> None:
    assert parse_phone(raw).e164 == "+213555123456"


def test_without_a_default_region_nonsense_is_still_reported_as_nonsense() -> None:
    assert _problem("call me maybe") is PhoneProblem.NOT_A_NUMBER
    assert _problem("+98765432109876") is PhoneProblem.NOT_A_NUMBER


# --------------------------------------------------------------------- allowed regions


def test_a_valid_number_from_a_country_outside_the_allowed_list_is_refused() -> None:
    only_dz = frozenset({"DZ"})
    assert (
        _problem("+212612345678", default_region="DZ", allowed_regions=only_dz)
        is PhoneProblem.REGION_NOT_ALLOWED
    )
    assert parse_phone("0555123456", default_region="DZ", allowed_regions=only_dz).region == "DZ"


def test_every_country_in_the_allowed_list_is_accepted() -> None:
    both = frozenset({"DZ", "MA"})
    assert parse_phone("+212612345678", default_region="DZ", allowed_regions=both).region == "MA"
    assert parse_phone("+213555123456", default_region="DZ", allowed_regions=both).region == "DZ"


def test_an_invalid_number_is_reported_as_invalid_not_as_the_wrong_country() -> None:
    """The message a customer gets must match their actual mistake."""
    assert (
        _problem("+98765432109876", default_region="DZ", allowed_regions=frozenset({"DZ"}))
        is PhoneProblem.NOT_A_NUMBER
    )


# --------------------------------------------------------------------- shared contacts


def test_a_number_known_to_be_international_needs_no_plus_sign() -> None:
    """Telegram delivers a shared contact as ``213555123456``."""
    assert parse_phone("213555123456", international=True).e164 == "+213555123456"
    assert parse_phone("+213555123456", international=True).e164 == "+213555123456"
    assert parse_phone("212612345678", default_region="DZ", international=True).region == "MA"


# --------------------------------------------------------------------- display helpers


def test_format_international_groups_a_stored_number_for_reading() -> None:
    assert format_international("+213555123456") == "+213 555 12 34 56"


@pytest.mark.parametrize("legacy", ["0555123456", "garbage", ""])
def test_format_international_leaves_an_unparseable_value_alone(legacy: str) -> None:
    """Rows written before validation existed must still display."""
    assert format_international(legacy) == legacy


def test_example_numbers_are_valid_for_their_own_country() -> None:
    """An example the bot itself would refuse would be worse than no example."""
    for region in ("DZ", "MA", "SA", "FR", "US"):
        local, international = example_numbers(region)
        assert parse_phone(local, default_region=region).region == region
        assert parse_phone(international).region == region


def test_example_numbers_fall_back_when_no_region_is_known() -> None:
    assert example_numbers("") == example_numbers("DZ")
    assert example_numbers("not-a-region") == example_numbers("DZ")


def test_describe_regions_lists_each_country_with_its_calling_code() -> None:
    assert describe_regions(("MA", "DZ")) == "DZ (+213), MA (+212)"
    assert describe_regions(frozenset({"SA"})) == "SA (+966)"


def test_is_supported_region_knows_real_country_codes_only() -> None:
    assert is_supported_region("DZ")
    assert not is_supported_region("dz")
    assert not is_supported_region("ZZ")
    assert not is_supported_region("")
