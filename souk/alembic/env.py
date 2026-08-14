import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool
from sqlalchemy import text
from sqlalchemy.engine import make_url

from alembic import context
from souk.db_schema import DEFAULT_DATABASE_URL, DEFAULT_DB_SCHEMA, quoted_schema
from souk.schema import metadata as souk_metadata

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# souk's schema is defined once as SQLAlchemy Core table metadata (see
# souk/schema.py). Pointing Alembic at it lets `alembic revision
# --autogenerate` diff future changes against that single definition, and
# renders dialect-correct DDL on both SQLite and Postgres.
target_metadata = souk_metadata

# Read SOUK_DATABASE_URL straight from the environment rather than
# constructing souk.config.CoreSettings: that would require the *whole* app
# config (token_signing_secret etc.), which has nothing to do with running
# migrations and would make this script fail on an unrelated missing var.
# Falls back to the same zero-config SQLite default the app uses
# (DEFAULT_DATABASE_URL, defined once in souk.db_schema) so `alembic upgrade
# head` works out of the box on a fresh checkout — a real Postgres
# deployment sets SOUK_DATABASE_URL explicitly (ideally a DDL-capable role).
database_url = os.environ.get("SOUK_DATABASE_URL", DEFAULT_DATABASE_URL)

# Alembic runs migrations synchronously, but the app's default SQLite URL
# names the async driver (`sqlite+aiosqlite`), which has no sync DBAPI.
# Swap it for the stdlib sync sqlite driver here so the migration engine
# can connect. Postgres's `postgresql+psycopg` driver already works both
# sync and async, so it's left untouched.
_url = make_url(database_url)
if _url.get_backend_name() == "sqlite" and _url.get_driver_name() == "aiosqlite":
    database_url = _url.set(drivername="sqlite").render_as_string(hide_password=False)

config.set_main_option("sqlalchemy.url", database_url)

# Same reasoning as SOUK_DATABASE_URL above for reading straight from the
# environment — but this one is fine to default, same as souk.config's own
# db_schema field (see souk/core.py). DEFAULT_DB_SCHEMA/quoted_schema
# come from souk.db_schema, not a locally re-typed "public" literal or
# quoting scheme — see that module for why.
db_schema = os.environ.get("SOUK_DB_SCHEMA", DEFAULT_DB_SCHEMA)

# Postgres schema isolation (CREATE SCHEMA / search_path) has no SQLite
# analog — SQLite has no schema namespace at all — so SOUK_DB_SCHEMA is
# ignored on a SQLite URL, matching souk/core.py. Only Postgres ever creates
# or targets a non-default schema; everything below keys off db_schema !=
# DEFAULT_DB_SCHEMA, so forcing it back to the default here is enough.
if make_url(database_url).get_backend_name() != "postgresql":
    db_schema = DEFAULT_DB_SCHEMA


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
        version_table_schema=db_schema if db_schema != DEFAULT_DB_SCHEMA else None,
    )

    with context.begin_transaction():
        if db_schema != DEFAULT_DB_SCHEMA:
            context.execute(f"CREATE SCHEMA IF NOT EXISTS {quoted_schema(db_schema)}")
            context.execute(f"SET search_path TO {quoted_schema(db_schema)}, public")
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
        if db_schema != DEFAULT_DB_SCHEMA:
            # Must happen before context.configure below: alembic checks
            # for (and creates, on a fresh DB) its own version-tracking
            # table in version_table_schema as soon as migrations start,
            # so the schema needs to already exist by then. Committed
            # immediately, outside the migration's own transaction, since
            # every migration statement after this depends on it.
            connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {quoted_schema(db_schema)}"))
            connection.execute(text(f"SET search_path TO {quoted_schema(db_schema)}, public"))
            connection.commit()

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema=db_schema if db_schema != DEFAULT_DB_SCHEMA else None,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
