from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from souk.config import settings

engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agents (
    name          TEXT PRIMARY KEY,
    sdk_client_id TEXT NOT NULL,
    agent_card    JSONB NOT NULL,
    joined_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS threads (
    thread_id         TEXT PRIMARY KEY,   -- souk-assigned: thread_<hex>
    agent_name        TEXT NOT NULL REFERENCES agents(name),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_activity_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS thread_messages (
    id            BIGSERIAL PRIMARY KEY,
    thread_id     TEXT NOT NULL REFERENCES threads(thread_id),
    run_id        TEXT NOT NULL,
    message_id    TEXT NOT NULL,
    seq           INT NOT NULL,
    message_json  JSONB NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (thread_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_thread_messages_thread ON thread_messages (thread_id, seq);

CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,   -- souk-assigned: run_<hex>
    thread_id     TEXT NOT NULL REFERENCES threads(thread_id),
    agent_name    TEXT NOT NULL REFERENCES agents(name),
    protocol      TEXT NOT NULL CHECK (protocol IN ('ag-ui','a2a')),
    status        TEXT NOT NULL CHECK (status IN ('queued','running','completed','failed','cancelled')) DEFAULT 'queued',
    input_json    JSONB NOT NULL,
    task_id       TEXT,   -- souk-assigned: task_<hex>, set only for protocol='a2a'
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at    TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_runs_agent_status ON runs (agent_name, status);
CREATE INDEX IF NOT EXISTS idx_runs_task_id ON runs (task_id);

CREATE TABLE IF NOT EXISTS run_events (
    id            BIGSERIAL PRIMARY KEY,
    run_id        TEXT NOT NULL REFERENCES runs(run_id),
    seq           INT NOT NULL,
    event_json    JSONB NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events (run_id, seq);
"""


async def bootstrap_schema() -> None:
    async with engine.begin() as conn:
        for statement in SCHEMA_SQL.strip().split(";\n\n"):
            statement = statement.strip().rstrip(";")
            if statement:
                await conn.exec_driver_sql(statement)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
