"""Order reference generation.

A reference has to survive being read aloud over the phone, typed back into a chat, and written
on a paper job sheet. So:

* ``TF-20260928-K7M2`` — prefix, date, four random characters.
* The random part uses a **Crockford-style alphabet with I, L, O and U removed**, so ``0``/``O``
  and ``1``/``I``/``L`` cannot be confused, and no accidental profanity appears.
* Comparison is case-insensitive and tolerant of a missing prefix or stray spaces, because
  customers will send ``k7m2`` or ``tf 20260928 k7m2``.
* ~1.05 million combinations per day, and generation retries on collision against the unique
  index, so the birthday problem is handled by the database rather than by hope.
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime
from typing import Final

PREFIX: Final = "TF"

#: Crockford base32 without I, L, O, U -- unambiguous when spoken or handwritten.
ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

SUFFIX_LENGTH: Final = 4

_REFERENCE_RE: Final = re.compile(
    rf"^{PREFIX}-(?P<date>\d{{8}})-(?P<suffix>[{ALPHABET}]{{{SUFFIX_LENGTH}}})$"
)

#: Characters a human might substitute, mapped back to the canonical alphabet.
_CONFUSABLES: Final = str.maketrans({"I": "1", "L": "1", "O": "0", "U": "V"})


def generate_reference(*, now: datetime | None = None) -> str:
    """Return a fresh reference such as ``TF-20260928-K7M2``.

    Uses :mod:`secrets` rather than :mod:`random`: a predictable reference would let anyone
    enumerate other customers' orders through the ``/status`` command.
    """
    moment = now or datetime.now(UTC)
    suffix = "".join(secrets.choice(ALPHABET) for _ in range(SUFFIX_LENGTH))
    return f"{PREFIX}-{moment.strftime('%Y%m%d')}-{suffix}"


def normalise_reference(raw: str) -> str | None:
    """Coerce customer input into a canonical reference, or ``None`` if it cannot be one.

    Accepts lower case, missing prefix, spaces or underscores instead of hyphens, and the
    common visual confusions.

    >>> normalise_reference("tf-20260928-k7m2")
    'TF-20260928-K7M2'
    >>> normalise_reference("20260928 K7M2")
    'TF-20260928-K7M2'
    >>> normalise_reference("hello")
    """
    if not raw:
        return None
    cleaned = raw.strip().upper().replace(" ", "-").replace("_", "-")
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    if not cleaned:
        return None
    if not cleaned.startswith(f"{PREFIX}-"):
        cleaned = f"{PREFIX}-{cleaned}"

    parts = cleaned.split("-")
    if len(parts) != 3:
        return None
    _, date_part, suffix_part = parts
    suffix_part = suffix_part.translate(_CONFUSABLES)
    candidate = f"{PREFIX}-{date_part}-{suffix_part}"
    return candidate if _REFERENCE_RE.match(candidate) else None


def is_valid_reference(raw: str) -> bool:
    """True when ``raw`` can be normalised to a well-formed reference."""
    return normalise_reference(raw) is not None
