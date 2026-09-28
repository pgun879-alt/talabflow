"""Tests for password hashing, JWT handling and rate limiting."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from talabflow.security import (
    MIN_PASSWORD_LENGTH,
    PasswordPolicyError,
    SlidingWindowRateLimiter,
    TokenError,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)

SECRET = "a-test-secret-that-is-comfortably-long-enough"


# --------------------------------------------------------------------- passwords


def test_a_password_verifies_against_its_own_hash() -> None:
    stored = hash_password("correct-horse-battery")
    assert verify_password("correct-horse-battery", stored)


def test_a_wrong_password_is_rejected() -> None:
    stored = hash_password("correct-horse-battery")
    assert not verify_password("wrong-horse-battery", stored)
    assert not verify_password("", stored)
    assert not verify_password("correct-horse-batter", stored)


def test_the_hash_does_not_contain_the_password() -> None:
    stored = hash_password("correct-horse-battery")
    assert "correct-horse-battery" not in stored


def test_the_same_password_hashes_differently_every_time() -> None:
    """A per-password random salt is what defeats rainbow tables and reveals nothing about
    which users share a password."""
    first = hash_password("correct-horse-battery")
    second = hash_password("correct-horse-battery")
    assert first != second
    assert verify_password("correct-horse-battery", first)
    assert verify_password("correct-horse-battery", second)


def test_the_hash_records_its_own_cost_parameters() -> None:
    """Parameters inside the record are what allow raising the cost later without
    invalidating every existing password."""
    stored = hash_password("correct-horse-battery")
    algorithm, n, r, p, salt, digest = stored.split("$")
    assert algorithm == "scrypt"
    assert int(n) >= 2**14
    assert int(r) == 8 and int(p) == 1
    assert salt and digest


def test_a_short_password_is_refused() -> None:
    with pytest.raises(PasswordPolicyError, match=str(MIN_PASSWORD_LENGTH)):
        hash_password("short")


@pytest.mark.parametrize(
    "corrupt",
    ["", "not-a-hash", "scrypt$broken", "bcrypt$1$2$3$4$5", "scrypt$x$y$z$AAAA$AAAA", "$$$$$"],
)
def test_a_corrupt_stored_hash_denies_access_instead_of_crashing(corrupt: str) -> None:
    """A damaged row must fail closed, not turn the login endpoint into a 500 that leaks the
    parse error."""
    assert verify_password("anything", corrupt) is False


def test_unicode_passwords_work() -> None:
    stored = hash_password("كلمة-المرور-الطويلة")
    assert verify_password("كلمة-المرور-الطويلة", stored)
    assert not verify_password("كلمة-المرور-القصيرة", stored)


# --------------------------------------------------------------------- tokens


def test_a_token_round_trips_its_claims() -> None:
    token = create_access_token(subject="amina", role="admin", secret=SECRET)
    claims = decode_access_token(token, secret=SECRET)
    assert claims.subject == "amina"
    assert claims.role == "admin"
    assert claims.token_id
    assert claims.expires_at > datetime.now(UTC)


def test_every_token_has_a_distinct_id() -> None:
    first = decode_access_token(
        create_access_token(subject="a", role="staff", secret=SECRET), secret=SECRET
    )
    second = decode_access_token(
        create_access_token(subject="a", role="staff", secret=SECRET), secret=SECRET
    )
    assert first.token_id != second.token_id


def test_a_token_signed_with_another_secret_is_rejected() -> None:
    # Both secrets are >= 32 bytes so PyJWT does not warn about HMAC key length; the point of
    # the test is the signature mismatch, not the key size.
    token = create_access_token(
        subject="amina", role="admin", secret="the-real-secret-is-long-enough-for-hs256"
    )
    with pytest.raises(TokenError, match="invalid"):
        decode_access_token(token, secret="a-different-secret-that-is-also-long-enough")


def test_a_tampered_token_is_rejected() -> None:
    token = create_access_token(subject="amina", role="staff", secret=SECRET)
    head, payload, signature = token.split(".")
    with pytest.raises(TokenError):
        decode_access_token(f"{head}.{payload}x.{signature}", secret=SECRET)


def test_an_expired_token_is_rejected_with_a_clear_message() -> None:
    expired = jwt.encode(
        {
            "sub": "amina",
            "role": "staff",
            "iat": int((datetime.now(UTC) - timedelta(hours=2)).timestamp()),
            "exp": int((datetime.now(UTC) - timedelta(hours=1)).timestamp()),
        },
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(TokenError, match="expired"):
        decode_access_token(expired, secret=SECRET)


def test_an_unsigned_token_is_rejected() -> None:
    """The classic JWT attack: alg=none. The decoder passes an explicit algorithm list, so a
    token's own header cannot choose how it is verified."""
    unsigned = jwt.encode({"sub": "amina", "role": "admin"}, key="", algorithm="none")
    with pytest.raises(TokenError):
        decode_access_token(unsigned, secret=SECRET)


def test_a_token_missing_required_claims_is_rejected() -> None:
    for payload in [
        {"role": "admin", "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp())},
        {"sub": "amina", "role": "admin"},  # no exp
    ]:
        token = jwt.encode(payload, SECRET, algorithm="HS256")
        with pytest.raises(TokenError):
            decode_access_token(token, secret=SECRET)


def test_a_token_without_a_role_claim_is_rejected() -> None:
    token = jwt.encode(
        {
            "sub": "amina",
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        },
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(TokenError, match="role"):
        decode_access_token(token, secret=SECRET)


def test_garbage_is_rejected() -> None:
    for value in ["", "not.a.token", "a.b.c", "x"]:
        with pytest.raises(TokenError):
            decode_access_token(value, secret=SECRET)


def test_the_ttl_is_honoured() -> None:
    claims = decode_access_token(
        create_access_token(subject="a", role="staff", secret=SECRET, ttl_minutes=5),
        secret=SECRET,
    )
    remaining = (claims.expires_at - datetime.now(UTC)).total_seconds()
    assert 240 < remaining <= 300


# --------------------------------------------------------------------- rate limiting


def test_requests_under_the_limit_pass() -> None:
    limiter = SlidingWindowRateLimiter(limit=3)
    for _ in range(3):
        allowed, retry_after = limiter.check("caller", now=100.0)
        assert allowed
        assert retry_after == 0.0


def test_the_request_over_the_limit_is_refused() -> None:
    limiter = SlidingWindowRateLimiter(limit=2)
    limiter.check("caller", now=100.0)
    limiter.check("caller", now=100.5)
    allowed, retry_after = limiter.check("caller", now=101.0)
    assert not allowed
    assert retry_after == pytest.approx(59.0)


def test_the_window_slides_instead_of_resetting() -> None:
    """A fixed window would let 2 x limit through across a boundary instant."""
    limiter = SlidingWindowRateLimiter(limit=2, window_seconds=60.0)
    assert limiter.check("caller", now=0.0)[0]
    assert limiter.check("caller", now=30.0)[0]
    assert not limiter.check("caller", now=59.0)[0]
    assert limiter.check("caller", now=61.0)[0]
    assert not limiter.check("caller", now=62.0)[0]


def test_limits_are_per_identity() -> None:
    limiter = SlidingWindowRateLimiter(limit=1)
    assert limiter.check("first", now=0.0)[0]
    assert not limiter.check("first", now=1.0)[0]
    assert limiter.check("second", now=1.0)[0]


def test_pruning_does_not_exempt_new_callers_from_the_limit() -> None:
    """Regression guard carried over from the same bug found in the sibling project: pruning
    after insertion deleted the new identity's own empty window, and the hit landed on a
    detached object -- so every caller past the threshold was never limited."""
    limiter = SlidingWindowRateLimiter(limit=2, window_seconds=60.0)
    for index in range(1100):
        limiter.check(f"warmup-{index}", now=0.0)
    assert limiter.check("fresh", now=1.0)[0]
    assert limiter.check("fresh", now=2.0)[0]
    assert not limiter.check("fresh", now=3.0)[0]


def test_idle_identities_are_pruned() -> None:
    limiter = SlidingWindowRateLimiter(limit=5, window_seconds=10.0)
    for index in range(1500):
        limiter.check(f"caller-{index}", now=0.0)
    assert len(limiter._windows) == 1500
    limiter.check("late", now=1000.0)
    assert len(limiter._windows) < 1500


def test_an_invalid_limit_is_refused() -> None:
    with pytest.raises(ValueError, match="limit must be positive"):
        SlidingWindowRateLimiter(limit=0)


def test_the_default_clock_is_used_when_none_is_supplied() -> None:
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=0.05)
    assert limiter.check("caller")[0]
    assert not limiter.check("caller")[0]
    time.sleep(0.06)
    assert limiter.check("caller")[0]
