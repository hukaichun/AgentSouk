import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool
from sqlalchemy import text

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# souk has no ORM models — schema lives in versions/*.py as raw SQL (same
# style as the old SCHEMA_SQL it replaced) — so there's no metadata for
# autogenerate to diff against; migrations are always hand-written.
target_metadata = None

# Read SOUK_DATABASE_URL straight from the environment rather than
# importing souk.config.settings: that pulls in the *whole* app config
# (token_signing_secret etc.), which has nothing to do with running
# migrations and, now that it has no default either, would make this
# script fail on an unrelated missing var. No fallback here either — the
# whole point is that DDL should always run against an explicitly chosen
# (ideally DDL-only) connection string, never a silently-reused default.
try:
    database_url = os.environ["SOUK_DATABASE_URL"]
except KeyError:
    raise RuntimeError(
        "SOUK_DATABASE_URL must be set to run migrations — no default. "
        "Point it at whatever DDL-capable connection this deployment uses "
        "for schema changes."
    ) from None
config.set_main_option("sqlalchemy.url", database_url)

# Same reasoning as SOUK_DATABASE_URL above for reading straight from the
# environment — but this one is fine to default, same as souk.config's own
# db_schema field (see souk/souk/db.py).
db_schema = os.environ.get("SOUK_DB_SCHEMA", "public")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        if db_schema != "public":
            # Must happen before context.configure below: alembic checks
            # for (and creates, on a fresh DB) its own version-tracking
            # table in version_table_schema as soon as migrations start,
            # so the schema needs to already exist by then. Committed
            # immediately, outside the migration's own transaction, since
            # every migration statement after this depends on it.
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{db_schema}"'))
            connection.execute(text(f'SET search_path TO "{db_schema}", public'))
            connection.commit()

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema=db_schema if db_schema != "public" else None,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
