from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from souk.config import settings
from souk.db_schema import DEFAULT_DB_SCHEMA, quoted_schema

# All of souk's raw SQL (repo.py) uses bare table names, not
# schema-qualified ones — pointing Postgres's search_path at
# settings.db_schema here is what makes those resolve into that schema
# instead of always meaning `public`. `public` stays second so shared
# extensions (pgcrypto — see the initial migration) are still reachable
# when db_schema is something else. Schema name must be quoted (no space
# after the comma — this is libpq's `options` argument-splitting, not SQL)
# or Postgres silently folds a mixed-case schema name to lowercase and
# every query 404s against a schema that doesn't exist.
_connect_args = (
    {"options": f"-c search_path={quoted_schema(settings.db_schema)},public"}
    if settings.db_schema != DEFAULT_DB_SCHEMA
    else {}
)

engine = create_async_engine(settings.database_url, pool_pre_ping=True, connect_args=_connect_args)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
