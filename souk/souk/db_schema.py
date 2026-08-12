"""Shared, dependency-free values needed by more than one souk entrypoint.

souk/souk/db.py (the running app) and souk/alembic/env.py (migrations) both
need to know the target Postgres schema and quote it the same way when
building search_path — but they can't both import souk.config.Settings:
env.py deliberately avoids it, since that would pull in unrelated required
settings like token_signing_secret just to run a migration. This module has
no other souk imports, so either side can depend on it safely, and the
default/quoting logic only needs to be correct in one place.
"""

DEFAULT_DB_SCHEMA = "public"


def quoted_schema(db_schema: str) -> str:
    """Double-quote a schema name so Postgres preserves its exact case.

    Must be used everywhere a schema name is interpolated into SQL or a
    connection's search_path — an unquoted copy anywhere silently folds a
    mixed-case schema name to lowercase, and that path ends up looking at
    a different (nonexistent) schema than the others.
    """
    return f'"{db_schema}"'
