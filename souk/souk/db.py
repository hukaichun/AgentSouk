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
    -- Free-form, souk-internal extension data — distinct from agent_card
    -- (which is protocol-facing: served verbatim as the A2A Agent Card).
    -- Not interpreted by souk itself; a place for callers/operators to
    -- attach whatever isn't worth a dedicated column.
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
    joined_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS threads (
    thread_id         TEXT PRIMARY KEY,   -- souk-assigned: thread_<hex>
    agent_name        TEXT NOT NULL REFERENCES agents(name),
    -- Set when this thread was spawned by an A2A call from within another
    -- thread's run (e.g. a main agent delegating to a sub-agent) — pure
    -- lineage, so "what conversation led to this one" is queryable. NULL
    -- for a thread started directly by a top-level caller.
    parent_thread_id  TEXT REFERENCES threads(thread_id),
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_activity_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_threads_parent ON threads (parent_thread_id);

-- A2A's task state and AG-UI's conversation history are two views onto the
-- same underlying conversation (a souk "thread" == an A2A session), so
-- they live in one table rather than split across a separate task/run
-- table and a separate message table. Each row is either:
--   kind='message'    — one AG-UI Message, part of the thread's history
--   kind='run_status' — the state of one run/A2A task within the thread
-- ordered together by `id` so the two interleave in true chronological
-- order. (run_id is NOT this table's primary key: a run_status row is one
-- among many rows sharing that run_id, since the messages it introduced
-- carry the same run_id.)
CREATE TABLE IF NOT EXISTS thread_history (
    id            BIGSERIAL PRIMARY KEY,
    thread_id     TEXT NOT NULL REFERENCES threads(thread_id),
    run_id        TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('message', 'run_status')),

    -- kind = 'message'
    message_id    TEXT,
    message_json  JSONB,

    -- kind = 'run_status'
    agent_name    TEXT,
    protocol      TEXT CHECK (protocol IN ('ag-ui', 'a2a')),
    status        TEXT CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled')),
    input_json    JSONB,
    task_id       TEXT,   -- souk-assigned: task_<hex>, set only for protocol='a2a'
    started_at    TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ,

    -- Free-form extension data, meaning depends on `kind`: for a message
    -- row, whatever metadata the AG-UI Message carried; for a run_status
    -- row, an A2A Task/Message's own `metadata` field when the caller was
    -- an A2A client (real A2A Tasks and Messages have one).
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (thread_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_thread_history_thread ON thread_history (thread_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_history_run_status_run_id
    ON thread_history (run_id) WHERE kind = 'run_status';
CREATE INDEX IF NOT EXISTS idx_thread_history_task_id
    ON thread_history (task_id) WHERE kind = 'run_status';

-- Finer-grained than thread_history's per-run status/history: the raw
-- AG-UI event stream for a run (tool calls, state deltas, ...), kept
-- separately since it's a different granularity than "conversation
-- history". Not FK'd to thread_history.run_id since that column isn't
-- uniquely constrained across the whole table (only among run_status
-- rows) — enforced at the application layer instead.
CREATE TABLE IF NOT EXISTS run_events (
    id            BIGSERIAL PRIMARY KEY,
    run_id        TEXT NOT NULL,
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
