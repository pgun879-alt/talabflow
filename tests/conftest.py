"""Shared fixtures.

Every fixture uses an isolated SQLite file and the offline scripted transport, so the suite
never touches the network, never needs a bot token, and leaves nothing behind.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from talabflow.bot import BotRunner
from talabflow.config import Settings
from talabflow.conversation import ConversationEngine
from talabflow.db import build_engine, build_session_factory, create_all, session_scope
from talabflow.models import Base, StaffRole
from talabflow.outbox import OutboxWorker
from talabflow.repository import create_staff_user
from talabflow.transports.scripted import ScriptedTransport

ADMIN_USERNAME = "admin-user"
ADMIN_PASSWORD = "admin-password-1"
STAFF_USERNAME = "staff-user"
STAFF_PASSWORD = "staff-password-1"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=os.environ.get("TALABFLOW_TEST_DATABASE_URL")
        or f"sqlite:///{tmp_path / 'test.sqlite3'}",
        transport="scripted",
        jwt_secret="test-secret-that-is-long-enough-for-hs256",
        environment="development",
        service_types=("Repair", "Installation", "Consultation", "Maintenance"),
        outbox_backoff_base_seconds=1,
        outbox_max_attempts=3,
    )


@pytest.fixture
def engine(settings: Settings) -> Iterator[Engine]:
    built = build_engine(settings)
    if not settings.is_sqlite:
        # A shared server database outlives the test, unlike a per-test SQLite file.
        Base.metadata.drop_all(built)
    create_all(built)
    yield built
    built.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return build_session_factory(engine)


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_scope(session_factory) as opened:
        yield opened


@pytest.fixture
def transport() -> ScriptedTransport:
    return ScriptedTransport()


@pytest.fixture
def conversation_engine(settings: Settings) -> ConversationEngine:
    return ConversationEngine(
        services=settings.service_types,
        business_name=settings.business_name,
        language=settings.default_language,
        max_message_length=settings.max_message_length,
    )


@pytest.fixture
def bot(
    settings: Settings, transport: ScriptedTransport, session_factory: sessionmaker[Session]
) -> BotRunner:
    return BotRunner(settings=settings, transport=transport, session_factory=session_factory)


@pytest.fixture
def worker(
    settings: Settings, transport: ScriptedTransport, session_factory: sessionmaker[Session]
) -> OutboxWorker:
    return OutboxWorker(settings=settings, transport=transport, session_factory=session_factory)


@pytest.fixture
def staff_users(session_factory: sessionmaker[Session]) -> None:
    """Create one admin and one staff user."""
    with session_scope(session_factory) as opened:
        create_staff_user(
            opened, username=ADMIN_USERNAME, password=ADMIN_PASSWORD, role=StaffRole.ADMIN
        )
        create_staff_user(
            opened, username=STAFF_USERNAME, password=STAFF_PASSWORD, role=StaffRole.STAFF
        )


#: The complete happy-path conversation, used by several tests.
HAPPY_PATH = [
    "/new",
    "1",
    "The washing machine will not drain properly",
    "0555123456",
    "12 Rue Didouche Mourad, Algiers",
    "yes",
]
