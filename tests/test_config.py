"""Tests for configuration validation.

The production-mode checks are the point of this file: each one is a real deployment mistake
that would otherwise go unnoticed until it caused harm.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from talabflow.config import MIN_SECRET_LENGTH, PLACEHOLDER_SECRET, Settings

GOOD_SECRET = "a-generated-secret-comfortably-over-thirty-two-characters"


def _base(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "database_url": "sqlite:///data/test.sqlite3",
        "transport": "scripted",
        "jwt_secret": GOOD_SECRET,
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # type: ignore[arg-type]


def test_defaults_are_offline_and_usable() -> None:
    settings = _base()
    assert settings.transport == "scripted"
    assert settings.telegram_bot_token is None
    assert settings.is_sqlite


def test_the_telegram_transport_requires_a_token() -> None:
    with pytest.raises(ValidationError, match="TELEGRAM_BOT_TOKEN"):
        _base(transport="telegram", telegram_bot_token=None)


def test_the_telegram_transport_with_a_token_is_accepted() -> None:
    assert _base(transport="telegram", telegram_bot_token="12345:ABC").transport == "telegram"


def test_an_unknown_transport_is_refused() -> None:
    with pytest.raises(ValidationError):
        _base(transport="carrier-pigeon")


# --------------------------------------------------------------------- production guards


def test_the_placeholder_secret_is_refused_in_production() -> None:
    """Shipping .env.example verbatim is the single most likely deployment mistake."""
    with pytest.raises(ValidationError, match="still the placeholder"):
        _base(environment="production", jwt_secret=PLACEHOLDER_SECRET)


def test_a_short_secret_is_refused_in_production() -> None:
    with pytest.raises(ValidationError, match=str(MIN_SECRET_LENGTH)):
        _base(environment="production", jwt_secret="too-short")


def test_a_wildcard_cors_origin_is_refused_in_production() -> None:
    with pytest.raises(ValidationError, match="must not be '\\*'"):
        _base(environment="production", cors_allow_origins="*")


def test_production_with_a_proper_secret_is_accepted() -> None:
    settings = _base(
        environment="production",
        jwt_secret=GOOD_SECRET,
        cors_allow_origins="https://staff.example.com",
    )
    assert settings.environment == "production"
    assert settings.cors_allow_origins == ("https://staff.example.com",)


def test_development_stays_permissive() -> None:
    """The guards must not make local development painful."""
    settings = _base(environment="development", jwt_secret=PLACEHOLDER_SECRET)
    assert settings.jwt_secret == PLACEHOLDER_SECRET


# --------------------------------------------------------------------- services


def test_an_empty_service_list_is_refused() -> None:
    with pytest.raises(ValidationError, match="at least one service"):
        _base(service_types="")


def test_duplicate_services_are_refused() -> None:
    """Duplicates would make two menu numbers mean the same thing."""
    with pytest.raises(ValidationError, match="must not contain duplicates"):
        _base(service_types="Repair,Repair")


def test_services_parse_from_a_plain_environment_string() -> None:
    settings = _base(service_types="Repair, Installation , Plumbing")
    assert settings.service_types == ("Repair", "Installation", "Plumbing")


def test_list_settings_parse_from_real_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pydantic-settings JSON-decodes sequence-typed fields before validators run, which turns
    a bare comma-separated value into a startup crash. NoDecode is what prevents that."""
    monkeypatch.setenv("TALABFLOW_JWT_SECRET", GOOD_SECRET)
    monkeypatch.setenv("TALABFLOW_SERVICE_TYPES", "Repair,Installation")
    monkeypatch.setenv("TALABFLOW_CORS_ALLOW_ORIGINS", "http://localhost:3000,https://x.example")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.service_types == ("Repair", "Installation")
    assert settings.cors_allow_origins == ("http://localhost:3000", "https://x.example")


# --------------------------------------------------------------------- ranges


def test_the_log_level_is_normalised_and_validated() -> None:
    assert _base(log_level="debug").log_level == "DEBUG"
    with pytest.raises(ValidationError, match="log_level must be one of"):
        _base(log_level="chatty")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("access_token_ttl_minutes", 0),
        ("access_token_ttl_minutes", 5000),
        ("user_messages_per_minute", 0),
        ("outbox_max_attempts", 0),
        ("outbox_batch_size", 0),
        ("outbox_backoff_base_seconds", 0),
        ("api_rate_limit_per_minute", 0),
        ("http_timeout_seconds", 0),
        ("telegram_poll_timeout_seconds", 61),
        ("max_message_length", 1),
        ("jwt_algorithm", "RS256"),
    ],
)
def test_out_of_range_values_are_refused(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        _base(**{field: value})


def test_secrets_are_absent_from_the_repr() -> None:
    settings = _base(transport="telegram", telegram_bot_token="12345:SECRETTOKEN")
    text = repr(settings)
    assert GOOD_SECRET not in text
    assert "SECRETTOKEN" not in text


def test_a_postgres_url_is_not_treated_as_sqlite() -> None:
    settings = _base(database_url="postgresql+psycopg://user:pass@localhost/talabflow")
    assert not settings.is_sqlite
