"""Alembic environment.

The database URL comes from the application's own settings rather than from ``alembic.ini``, so
there is exactly one source of truth and no connection string (possibly containing a password)
is ever committed.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from talabflow.config import Settings
from talabflow.models import Base, UtcDateTime

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = Settings()
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def render_item(type_: str, obj: object, autogen_context: object) -> str | bool:
    """Render application-specific column types as their plain SQLAlchemy equivalents.

    A migration has to keep working years later, after application classes have been renamed,
    moved or deleted -- so a migration should never import application code. Autogenerate would
    otherwise emit ``talabflow.models.UtcDateTime(timezone=True)`` and fail with ``NameError``.

    ``UtcDateTime`` exists purely to normalise timezones **in Python**; its DDL is identical to
    ``DateTime(timezone=True)``, so that is what gets written to the migration.
    """
    if type_ == "type" and isinstance(obj, UtcDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting, for review or manual application."""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_item=render_item,
            # Required for SQLite: it cannot ALTER most things, so Alembic rewrites the table.
            render_as_batch=settings.is_sqlite,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
