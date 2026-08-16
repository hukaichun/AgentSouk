"""Shared, dependency-free values needed by more than one souk entrypoint.

souk/souk/core.py (the running app) and souk/alembic/env.py (migrations) both
need to know the target Postgres schema and quote it the same way when
building search_path — but they can't both import souk.config.Settings:
env.py deliberately avoids it, since that would pull in unrelated required
settings like token_signing_secret just to run a migration. This module has
no other souk imports, so either side can depend on it safely, and the
default/quoting logic only needs to be correct in one place.
"""

DEFAULT_DB_SCHEMA = "public"

# souk's zero-config default backend: a local SQLite file. Defined here,
# once, because both the running app (souk.config.Settings.database_url) and
# migrations (souk/alembic/env.py) need the same default and env.py
# deliberately can't import souk.config. `+aiosqlite` is the async driver
# the app engine uses; env.py swaps it for the sync sqlite driver since
# Alembic runs migrations synchronously. See souk/config.py for why SQLite
# is the default and when to point SOUK_DATABASE_URL at Postgres instead.
DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./souk.db"

# The migration this code expects the database to be at — compared against
# `alembic_version` by Souk.health, which is how a process notices it is
# pointed at a database nobody has migrated yet, rather than discovering it
# as a missing column halfway through a request.
#
# A literal rather than a lookup, because souk/alembic/ is not shipped inside
# the package (see pyproject's `packages = ["souk"]`): an installed souk has
# no migration directory to read a head from. It cannot drift, though —
# tests/test_schema_revision.py fails if it stops matching alembic's actual
# head, which is checkable in the repo where the directory does exist.
EXPECTED_SCHEMA_REVISION = "6f326dc19242"


def quoted_schema(db_schema: str) -> str:
    """Double-quote a schema name so Postgres preserves its exact case.

    Must be used everywhere a schema name is interpolated into SQL or a
    connection's search_path — an unquoted copy anywhere silently folds a
    mixed-case schema name to lowercase, and that path ends up looking at
    a different (nonexistent) schema than the others.
    """
    return f'"{db_schema}"'
