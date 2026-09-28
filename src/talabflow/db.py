"""Engine and session management."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .models import Base

logger = logging.getLogger(__name__)


def build_engine(settings: Settings) -> Engine:
    """Create the engine for ``settings.database_url``.

    SQLite needs two specific accommodations, both of which are silent data-integrity bugs if
    omitted:

    * ``check_same_thread=False``, because FastAPI runs sync endpoints in a thread pool;
    * ``PRAGMA foreign_keys=ON`` **per connection**, since SQLite defaults it off and every
      ``ON DELETE`` rule in :mod:`.models` would otherwise be decorative.
    """
    connect_args: dict[str, object] = {}
    if settings.is_sqlite:
        connect_args["check_same_thread"] = False
        _ensure_sqlite_directory(settings.database_url)

    engine = create_engine(
        settings.database_url,
        echo=settings.sql_echo,
        future=True,
        connect_args=connect_args,
        pool_pre_ping=not settings.is_sqlite,
    )

    if settings.is_sqlite:

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection: object, _record: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            # Wait rather than failing instantly when the bot and the worker both write.
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine


def _ensure_sqlite_directory(database_url: str) -> None:
    """Create the parent directory of a SQLite file so first run does not fail."""
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return
    path = database_url[len(prefix) :]
    if path and path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Session factory with autoflush off.

    Autoflush is disabled deliberately: with it on, a read inside a half-built unit of work can
    flush an incomplete object and trip a NOT NULL constraint far from the real cause.
    """
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Run a block in one transaction: commit on success, roll back on any exception."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all(engine: Engine) -> None:
    """Create the schema directly from the models.

    Used by tests and by ``talabflow init-db`` for a quick start. Alembic remains the migration
    path of record for anything long-lived -- see ``migrations/``.
    """
    Base.metadata.create_all(engine)
