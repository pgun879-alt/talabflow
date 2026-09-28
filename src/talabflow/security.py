"""Password hashing, JWT issuing and verification, and flood control.

Password hashing uses :func:`hashlib.scrypt` from the standard library rather than ``bcrypt``
or ``argon2-cffi``. The reasoning, since it is an unusual-looking choice:

* ``scrypt`` is memory-hard, which is the property that matters against GPU cracking, and it
  is specified in RFC 7914.
* It is in the standard library, so there is no compiled dependency to install on a student's
  laptop and nothing extra to keep patched.
* The cost parameters are stored *inside the hash string*, so they can be raised later without
  invalidating existing passwords.

Argon2id would be the better choice in an environment where adding a dependency is free. That
trade-off is stated in the README rather than hidden.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import jwt

logger = logging.getLogger(__name__)

# RFC 7914 parameters. n=2**14 with r=8, p=1 costs roughly 16 MB and a few milliseconds --
# comfortable for a login endpoint, expensive in bulk for an attacker.
_SCRYPT_N: Final = 2**14
_SCRYPT_R: Final = 8
_SCRYPT_P: Final = 1
_SCRYPT_DKLEN: Final = 32
_SALT_BYTES: Final = 16
_ALGORITHM_TAG: Final = "scrypt"

MIN_PASSWORD_LENGTH: Final = 10


class PasswordPolicyError(ValueError):
    """Raised when a proposed password does not meet the minimum policy."""


class TokenError(Exception):
    """Raised when a token is missing, malformed, expired or otherwise unusable."""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def hash_password(password: str) -> str:
    """Hash ``password`` into a self-describing string.

    Format: ``scrypt$n$r$p$salt$hash``. Carrying the parameters means a future increase in cost
    does not invalidate stored hashes -- :func:`verify_password` reads them from the record.

    Raises:
        PasswordPolicyError: if the password is shorter than :data:`MIN_PASSWORD_LENGTH`.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters long"
        )
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=64 * 1024 * 1024,
    )
    return f"{_ALGORITHM_TAG}${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(derived)}"


def verify_password(password: str, stored: str) -> bool:
    """Check ``password`` against a hash produced by :func:`hash_password`.

    Returns ``False`` rather than raising on a malformed record: a corrupt row must deny access,
    not crash the login endpoint into a 500 that leaks the parse error.
    """
    try:
        tag, n_text, r_text, p_text, salt_text, hash_text = stored.split("$")
        if tag != _ALGORITHM_TAG:
            return False
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_unb64(salt_text),
            n=int(n_text),
            r=int(r_text),
            p=int(p_text),
            dklen=len(_unb64(hash_text)),
            maxmem=64 * 1024 * 1024,
        )
    except (ValueError, TypeError, MemoryError):
        logger.warning("could not parse a stored password hash; denying access")
        return False
    return hmac.compare_digest(derived, _unb64(hash_text))


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """The claims this application relies on."""

    subject: str
    role: str
    token_id: str
    expires_at: datetime


def create_access_token(
    *,
    subject: str,
    role: str,
    secret: str,
    algorithm: str = "HS256",
    ttl_minutes: int = 60,
) -> str:
    """Issue a short-lived signed access token.

    Includes ``jti`` so a specific token can be identified in logs, and ``iat``/``nbf`` so a
    token cannot be presented before it was issued.
    """
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=ttl_minutes)).timestamp()),
    }
    return jwt.encode(payload, secret, algorithm=algorithm)


def decode_access_token(token: str, *, secret: str, algorithm: str = "HS256") -> TokenClaims:
    """Verify and decode a token.

    The allowed algorithm is passed explicitly and as a single-item list. Accepting whatever
    the token's own header claims is the classic JWT vulnerability -- a forged ``alg: none``
    or an HS256 token verified against an RSA public key.

    Raises:
        TokenError: if the signature, expiry, or required claims are invalid.
    """
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[algorithm],
            options={"require": ["exp", "sub", "iat"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("token is invalid") from exc

    subject = payload.get("sub")
    role = payload.get("role")
    if not isinstance(subject, str) or not isinstance(role, str):
        raise TokenError("token is missing the sub or role claim")
    return TokenClaims(
        subject=subject,
        role=role,
        token_id=str(payload.get("jti", "")),
        expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=UTC),
    )


@dataclass(slots=True)
class _Window:
    hits: deque[float]


_PRUNE_THRESHOLD: Final = 1024


class SlidingWindowRateLimiter:
    """Per-identity sliding-window limiter, used for both API calls and inbound chat messages.

    A sliding window rather than a fixed one, because a fixed window allows ``2 * limit``
    requests either side of a boundary instant.
    """

    def __init__(self, *, limit: int, window_seconds: float = 60.0) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self._windows: dict[str, _Window] = {}
        self._lock = threading.Lock()

    def check(self, identity: str, *, now: float | None = None) -> tuple[bool, float]:
        """Record a hit. Returns ``(allowed, retry_after_seconds)``."""
        timestamp = now if now is not None else time.monotonic()
        cutoff = timestamp - self.window_seconds
        with self._lock:
            window = self._windows.get(identity)
            if window is None:
                # Prune before inserting and only over pre-existing entries: pruning after
                # insertion would delete this identity's brand-new empty window, and the hit
                # would land on a detached object -- exempting every new caller from the limit.
                if len(self._windows) >= _PRUNE_THRESHOLD:
                    self._prune(cutoff)
                window = _Window(hits=deque())
                self._windows[identity] = window
            else:
                while window.hits and window.hits[0] <= cutoff:
                    window.hits.popleft()
            if len(window.hits) >= self.limit:
                return False, max(window.hits[0] + self.window_seconds - timestamp, 0.0)
            window.hits.append(timestamp)
            return True, 0.0

    def _prune(self, cutoff: float) -> None:
        """Drop identities with no hits inside the window. Caller must hold the lock."""
        stale = [
            identity
            for identity, window in self._windows.items()
            if not window.hits or window.hits[-1] <= cutoff
        ]
        for identity in stale:
            del self._windows[identity]
