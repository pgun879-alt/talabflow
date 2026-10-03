"""Application configuration, validated at startup.

The guiding rule: a misconfigured deployment must fail loudly before it accepts a single
order, never silently run in an unsafe state. Several validators below exist specifically to
make an insecure production setup impossible rather than merely discouraged.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

TransportName = Literal["scripted", "telegram"]

#: A tuple-of-strings setting supplied as a plain comma-separated environment value.
#: ``pydantic-settings`` JSON-decodes sequence-typed fields before validators run, so a bare
#: ``a,b`` would be a startup crash without ``NoDecode``.
CommaSeparated = Annotated[tuple[str, ...], NoDecode]

#: Minimum length for the JWT signing secret. 32 bytes of entropy is the floor at which
#: brute-forcing an HS256 key stops being a realistic attack.
MIN_SECRET_LENGTH = 32

#: The placeholder shipped in .env.example. Refused outright in production so nobody deploys
#: it. Not a secret -- it exists precisely so that using it as one fails loudly.
PLACEHOLDER_SECRET = "change-me"  # noqa: S105


class Settings(BaseSettings):
    """Validated runtime configuration."""

    model_config = SettingsConfigDict(
        env_prefix="TALABFLOW_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- environment -------------------------------------------------------------
    environment: Literal["development", "production"] = "development"

    # --- database ----------------------------------------------------------------
    database_url: str = Field(
        default="sqlite:///data/talabflow.sqlite3",
        description="SQLAlchemy URL. SQLite by default; PostgreSQL works unchanged.",
    )
    sql_echo: bool = False

    # --- authentication ----------------------------------------------------------
    jwt_secret: str = Field(
        default=PLACEHOLDER_SECRET,
        repr=False,
        description="HS256 signing secret. Generate with: python3 -c "
        "'import secrets; print(secrets.token_urlsafe(48))'",
    )
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    access_token_ttl_minutes: int = Field(default=60, ge=1, le=1440)

    # --- messaging ---------------------------------------------------------------
    transport: TransportName = Field(
        default="scripted",
        description="'scripted' is an offline in-memory transport for demos and tests. "
        "'telegram' talks to the real Bot API and needs a bot token.",
    )
    telegram_bot_token: str | None = Field(default=None, repr=False)
    telegram_api_base: str = "https://api.telegram.org"
    telegram_poll_timeout_seconds: int = Field(default=25, ge=0, le=60)
    http_timeout_seconds: float = Field(default=30.0, gt=0, le=300)

    # --- bot behaviour -----------------------------------------------------------
    business_name: str = Field(default="Nour Technical Services", max_length=120)
    default_language: Literal["en", "ar"] = "en"
    service_types: CommaSeparated = ("Repair", "Installation", "Consultation", "Maintenance")
    user_messages_per_minute: int = Field(
        default=20, ge=1, le=600, description="Per-customer flood control for inbound messages."
    )
    max_message_length: int = Field(default=1000, ge=10, le=8000)

    # --- outbox worker -----------------------------------------------------------
    outbox_batch_size: int = Field(default=20, ge=1, le=500)
    outbox_max_attempts: int = Field(default=5, ge=1, le=20)
    outbox_backoff_base_seconds: int = Field(default=30, ge=1, le=3600)
    outbox_poll_interval_seconds: float = Field(default=5.0, gt=0, le=600)
    outbox_lease_seconds: int = Field(
        default=120,
        ge=5,
        le=3600,
        description="How long a worker's claim on a message is respected. After this, another "
        "worker may reclaim it -- which is how a message survives the worker that claimed it "
        "crashing. Must comfortably exceed the time one send can take, or a slow send will be "
        "reclaimed and delivered twice.",
    )
    worker_id: str = Field(
        default="",
        description="Identifies this worker in outbox claims. Defaults to hostname:pid, which is "
        "unique per process and readable when inspecting the table by hand.",
    )

    # --- admin API ---------------------------------------------------------------
    api_rate_limit_per_minute: int = Field(default=120, ge=1, le=10_000)
    login_attempts_per_minute: int = Field(
        default=10,
        ge=1,
        le=600,
        description="Login attempts allowed per account per minute, counted whether or not they "
        "succeed. The general API limit only applies after a token is verified, so without this "
        "the login endpoint would accept unlimited password guesses.",
    )
    cors_allow_origins: CommaSeparated = ()

    # --- logging -----------------------------------------------------------------
    log_level: str = "INFO"

    @field_validator("service_types", "cors_allow_origins", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("log_level")
    @classmethod
    def _valid_log_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {value!r}")
        return upper

    @field_validator("service_types")
    @classmethod
    def _at_least_one_service(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("TALABFLOW_SERVICE_TYPES must list at least one service")
        if len(set(value)) != len(value):
            raise ValueError("TALABFLOW_SERVICE_TYPES must not contain duplicates")
        return value

    @model_validator(mode="after")
    def _default_worker_id(self) -> Settings:
        if not self.worker_id:
            import os
            import socket

            object.__setattr__(self, "worker_id", f"{socket.gethostname()}:{os.getpid()}"[:64])
        return self

    @model_validator(mode="after")
    def _lease_outlives_a_send(self) -> Settings:
        """A lease shorter than one send attempt would let a slow send be reclaimed mid-flight.

        That is the one way this design could produce a duplicate message, so it is refused at
        startup rather than left as a footgun.
        """
        if self.outbox_lease_seconds <= self.http_timeout_seconds:
            raise ValueError(
                f"TALABFLOW_OUTBOX_LEASE_SECONDS ({self.outbox_lease_seconds}) must be greater "
                f"than TALABFLOW_HTTP_TIMEOUT_SECONDS ({self.http_timeout_seconds}), otherwise a "
                "slow send can outlive its own lease and be reclaimed and re-sent by another "
                "worker"
            )
        return self

    @model_validator(mode="after")
    def _check_coherence(self) -> Settings:
        if self.transport == "telegram" and not self.telegram_bot_token:
            raise ValueError(
                "TALABFLOW_TRANSPORT=telegram requires TALABFLOW_TELEGRAM_BOT_TOKEN. Use "
                "TALABFLOW_TRANSPORT=scripted to run the full flow with no token at all."
            )
        if self.environment == "production":
            # These three checks are the reason this validator exists. Each one is a real
            # deployment mistake that would otherwise go unnoticed until it caused harm.
            if self.jwt_secret == PLACEHOLDER_SECRET:
                raise ValueError(
                    "TALABFLOW_JWT_SECRET is still the placeholder from .env.example. Generate "
                    "one: python3 -c 'import secrets; print(secrets.token_urlsafe(48))'"
                )
            if len(self.jwt_secret) < MIN_SECRET_LENGTH:
                raise ValueError(
                    f"TALABFLOW_JWT_SECRET must be at least {MIN_SECRET_LENGTH} characters in "
                    f"production (got {len(self.jwt_secret)})"
                )
            if "*" in self.cors_allow_origins:
                raise ValueError(
                    "TALABFLOW_CORS_ALLOW_ORIGINS must not be '*' in production; list the exact "
                    "origins that are allowed to call the admin API"
                )
        return self

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, constructed once."""
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cached settings. Used by tests that manipulate the environment."""
    get_settings.cache_clear()
