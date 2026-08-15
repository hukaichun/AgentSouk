from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from souk.config import settings
from souk.db_schema import DEFAULT_DB_SCHEMA, quoted_schema

# souk supports two backends off the same code (see souk/schema.py and
# souk/repo.py, which are written against SQLAlchemy Core so the SQL is
# dialect-neutral): SQLite for zero-config dev/CI/single-node, Postgres for
# a real multi-writer gateway. Which one is chosen purely by the scheme of
# settings.database_url — `sqlite+aiosqlite://…` vs `postgresql+psycopg://…`.
_backend = make_url(settings.database_url).get_backend_name()
_is_sqlite = _backend == "sqlite"

# Postgres schema isolation: all of souk's SQL uses bare table names, so
# pointing search_path at settings.db_schema is what makes those resolve
# into that schema instead of `public`. `public` stays second so shared
# extensions stay reachable. Schema name must be quoted (no space after the
# comma — this is libpq's `options` argument-splitting, not SQL) or
# Postgres silently folds a mixed-case schema name to lowercase and every
# query 404s. SQLite has no schema namespace, so db_schema is ignored there
# (a non-default value on a SQLite URL is a no-op, per config.py's note).
_connect_args = (
    {"options": f"-c search_path={quoted_schema(settings.db_schema)},public"}
    if not _is_sqlite and settings.db_schema != DEFAULT_DB_SCHEMA
    else {}
)

# pool_pre_ping guards against stale server connections — pointless for a
# local SQLite file, and SQLite's default pool doesn't use it meaningfully.
engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=not _is_sqlite,
    connect_args=_connect_args,
)


if _is_sqlite:

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        """Per-connection SQLite setup. SQLite defaults are tuned for an
        embedded single-process store, not a concurrent server:

        - `foreign_keys=ON`: SQLite ignores FK constraints unless asked to
          enforce them per connection. souk's schema (threads → agents,
          thread_history → threads, …) relies on them, same as Postgres
          enforces by default.
        - `journal_mode=WAL`: lets readers proceed while a single writer is
          active, which softens (does not remove) SQLite's one-writer-at-a-
          time limit — the reason SQLite is positioned for low-concurrency
          use, not a busy gateway. Persists on the database file once set.
        - `busy_timeout=5000`: wait up to 5s for a held write lock before
          raising "database is locked", instead of failing instantly under
          souk's overlapping writers (request handlers + health sweeps).
        """
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
