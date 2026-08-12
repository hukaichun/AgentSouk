from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from souk.config import settings

# All of souk's raw SQL (repo.py) uses bare table names, not
# schema-qualified ones — pointing Postgres's search_path at
# settings.db_schema here is what makes those resolve into that schema
# instead of always meaning `public`. `public` stays second so shared
# extensions (pgcrypto — see the initial migration) are still reachable
# when db_schema is something else.
_connect_args = (
    {"options": f"-c search_path={settings.db_schema},public"}
    if settings.db_schema != "public"
    else {}
)

engine = create_async_engine(settings.database_url, pool_pre_ping=True, connect_args=_connect_args)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
