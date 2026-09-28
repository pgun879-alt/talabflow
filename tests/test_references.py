"""Tests for order references: format, uniqueness, and tolerance of human input."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from talabflow.references import (
    ALPHABET,
    PREFIX,
    SUFFIX_LENGTH,
    generate_reference,
    is_valid_reference,
    normalise_reference,
)


def test_generated_references_have_the_documented_shape() -> None:
    reference = generate_reference(now=datetime(2026, 9, 28, tzinfo=UTC))
    assert reference.startswith(f"{PREFIX}-20260928-")
    suffix = reference.rsplit("-", 1)[1]
    assert len(suffix) == SUFFIX_LENGTH
    assert all(character in ALPHABET for character in suffix)


def test_the_alphabet_excludes_confusable_characters() -> None:
    """I/L/O/U are excluded so a reference survives being read aloud or handwritten -- and so
    no accidental word appears in a customer-facing code."""
    for character in "ILOU":
        assert character not in ALPHABET


def test_generated_references_are_mostly_distinct() -> None:
    references = {generate_reference() for _ in range(500)}
    # ~1.05m combinations per day, so 500 draws should essentially never collide.
    assert len(references) >= 498


def test_a_generated_reference_validates_and_round_trips() -> None:
    reference = generate_reference()
    assert is_valid_reference(reference)
    assert normalise_reference(reference) == reference


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("TF-20260928-K7M2", "TF-20260928-K7M2"),
        ("tf-20260928-k7m2", "TF-20260928-K7M2"),
        ("  TF-20260928-K7M2  ", "TF-20260928-K7M2"),
        ("20260928-K7M2", "TF-20260928-K7M2"),
        ("TF 20260928 K7M2", "TF-20260928-K7M2"),
        ("TF_20260928_K7M2", "TF-20260928-K7M2"),
        ("TF--20260928--K7M2", "TF-20260928-K7M2"),
    ],
)
def test_normalisation_tolerates_how_customers_actually_type(raw: str, expected: str) -> None:
    assert normalise_reference(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("TF-20260928-K7MO", "TF-20260928-K7M0"),  # letter O -> digit zero
        ("TF-20260928-K7MI", "TF-20260928-K7M1"),  # letter I -> digit one
        ("TF-20260928-K7ML", "TF-20260928-K7M1"),  # letter L -> digit one
        ("TF-20260928-K7MU", "TF-20260928-K7MV"),  # U -> V
    ],
)
def test_visually_confusable_characters_are_corrected(raw: str, expected: str) -> None:
    """Someone reading a reference off a screen will type O for 0. Correcting that is the
    difference between a customer getting their status and giving up."""
    assert normalise_reference(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "hello",
        "TF-2026-K7M2",  # date too short
        "TF-20260928-K7M",  # suffix too short
        "TF-20260928-K7M2X",  # suffix too long
        "TF-20260928",  # no suffix
        "TF-ABCDEFGH-K7M2",  # non-numeric date
        "TF-20260928-K7M2-EXTRA",
        "' OR 1=1 --",
        "../../etc/passwd",
    ],
)
def test_input_that_cannot_be_a_reference_is_rejected(raw: str) -> None:
    assert normalise_reference(raw) is None
    assert not is_valid_reference(raw)
