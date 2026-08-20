import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool
from sqlalchemy import text
from sqlalchemy.engine import make_url

from alembic import context
from funduq.db_schema import DEFAULT_DATABASE_URL, DEFAULT_DB_SCHEMA, quoted_schema
from funduq.schema import metadata as funduq_metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = funduq_metadata

database_url = config.attributes.get("funduq_database_url") or os.environ.get(
    "FUNDUQ_DATABASE_URL", DEFAULT_DATABASE_URL
)

_url = make_url(database_url)
if _url.get_backend_name() == "sqlite" and _url.get_driver_name() == "aiosqlite":
    database_url = _url.set(drivername="sqlite").render_as_string(hide_password=False)

config.set_main_option("sqlalchemy.url", database_url)

db_schema = config.attributes.get("funduq_db_schema") or os.environ.get(
    "FUNDUQ_DB_SCHEMA", DEFAULT_DB_SCHEMA
)

if make_url(database_url).get_backend_name() != "postgresql":
    db_schema = DEFAULT_DB_SCHEMA


def run_migrations_offline() -> None:
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
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        if db_schema != DEFAULT_DB_SCHEMA:
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
