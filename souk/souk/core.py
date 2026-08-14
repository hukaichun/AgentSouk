"""The `Souk` object: one configured souk instance.

This is what replaces the import-time globals `souk.config.settings`,
`souk.db.engine` and `souk.db.SessionLocal`. A `Souk` owns its settings and
its own database engine, so constructing one is the moment configuration is
resolved — not the moment some module is imported. Several souks with
different settings can therefore coexist in one process, and a test can build
one directly instead of arranging environment variables before the first
import.

Deliberately network-free: this module knows about a database and nothing
else. See docs/library-architecture.md.

For now `Souk` is the database/settings holder; the domain methods
(start_run, get_thread, list_agents, …) land here in a later step of that
document's migration.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from souk.config import CoreSettings
from souk.db_schema import DEFAULT_DB_SCHEMA, quoted_schema


class Souk:
    """One configured souk. Construct with explicit settings, or with none
    to resolve them from the `SOUK_*` environment variables:

        souk = Souk()                                    # all from env
        souk = Souk(CoreSettings(database_url="..."))    # explicit
    """

    def __init__(self, settings: CoreSettings | None = None) -> None:
        self.settings = settings or CoreSettings()
        self.engine = _create_engine(self.settings)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A database session scoped to a block — the direct replacement for
        `async with SessionLocal() as session:`."""
        async with self.sessionmaker() as session:
            yield session

    async def dispose(self) -> None:
        """Release the connection pool. Worth calling when a Souk is
        discarded before the process exits — several instances in one
        process is now a supported case, so their pools shouldn't outlive
        them."""
        await self.engine.dispose()


def _create_engine(settings: CoreSettings):
    """souk supports two backends off the same code (see souk/schema.py and
    souk/repo.py, which are written against SQLAlchemy Core so the SQL is
    dialect-neutral): SQLite for zero-config dev/CI/single-node, Postgres for
    a real multi-writer gateway. Which one is chosen purely by the scheme of
    settings.database_url — `sqlite+aiosqlite://…` vs `postgresql+psycopg://…`.
    """
    is_sqlite = make_url(settings.database_url).get_backend_name() == "sqlite"

    # Postgres schema isolation: all of souk's SQL uses bare table names, so
    # pointing search_path at settings.db_schema is what makes those resolve
    # into that schema instead of `public`. `public` stays second so shared
    # extensions stay reachable. Schema name must be quoted (no space after
    # the comma — this is libpq's `options` argument-splitting, not SQL) or
    # Postgres silently folds a mixed-case schema name to lowercase and every
    # query 404s. SQLite has no schema namespace, so db_schema is ignored
    # there (a non-default value on a SQLite URL is a no-op, per config.py).
    connect_args = (
        {"options": f"-c search_path={quoted_schema(settings.db_schema)},public"}
        if not is_sqlite and settings.db_schema != DEFAULT_DB_SCHEMA
        else {}
    )

    # pool_pre_ping guards against stale server connections — pointless for a
    # local SQLite file, and SQLite's default pool doesn't use it meaningfully.
    engine = create_async_engine(
        settings.database_url,
        pool_pre_ping=not is_sqlite,
        connect_args=connect_args,
    )

    if is_sqlite:

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
            """Per-connection SQLite setup. SQLite defaults are tuned for an
            embedded single-process store, not a concurrent server:

            - `foreign_keys=ON`: SQLite ignores FK constraints unless asked
              to enforce them per connection. souk's schema (threads →
              agents, thread_history → threads, …) relies on them, same as
              Postgres enforces by default.
            - `journal_mode=WAL`: lets readers proceed while a single writer
              is active, which softens (does not remove) SQLite's
              one-writer-at-a-time limit — the reason SQLite is positioned
              for low-concurrency use, not a busy gateway. Persists on the
              database file once set.
            - `busy_timeout=5000`: wait up to 5s for a held write lock before
              raising "database is locked", instead of failing instantly
              under souk's overlapping writers (request handlers + health
              sweeps).
            """
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine
