from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from souk.config import settings

engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

SCHEMA_SQL = """
-- Every souk-owned entity id (agent_id, thread_id, run_id, message_id)
-- is generated here, by the database, at insert time — never
-- precomputed in Python and handed to Postgres as a value to store.
-- A2A has no separate task_id concept; its Task.id is just this run_id
-- (see api_a2a._start_run). `gen_random_bytes` needs pgcrypto.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE OR REPLACE FUNCTION souk_new_id(category TEXT) RETURNS TEXT AS $$
    SELECT category || '_' || encode(gen_random_bytes(12), 'hex');
$$ LANGUAGE sql VOLATILE;

-- A "provider" isn't a first-class registration concept the way an agent
-- is — it's just whoever holds a given public_key, identified purely by
-- that key (see agents.public_key). This table exists only to attach an
-- optional, non-unique display label to that key for humans browsing the
-- directory (souk-directory groups agents by public_key into
-- "storefronts"). Deliberately not folded into `agents`: a provider's
-- name is set once per key, not once per agent, and shouldn't get wiped
-- out just because one particular registration batch didn't happen to
-- pass it (see repo.upsert_provider_name).
CREATE TABLE IF NOT EXISTS providers (
    public_key    TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agents (
    -- Database-generated (souk_new_id('agent') — see repo.register_agents,
    -- which only ever supplies an explicit value here to *reuse* an
    -- already-existing (public_key, name) pair's id, never to mint a new
    -- one itself) — the real routing/ownership key. `name` below is
    -- deliberately NOT this: it's a free, non-unique, human-facing label
    -- (multiple identities may register the same name; see the
    -- UNIQUE(public_key, name) constraint instead).
    agent_id      TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    sdk_client_id TEXT NOT NULL,
    -- Ed25519 public key (hex) that owns this agent_id — see
    -- souk/identity.py. Set once at first registration of this
    -- (public_key, name) pair, never changed by a later one. Whoever holds
    -- the matching private key owns this agent_id; `name` itself is not
    -- exclusive, so a different public_key may freely reuse the same name.
    public_key    TEXT NOT NULL,
    agent_card    JSONB NOT NULL,
    -- Free-form, souk-internal extension data — distinct from agent_card
    -- (which is protocol-facing: served verbatim as the A2A Agent Card).
    -- Not interpreted by souk itself; a place for callers/operators to
    -- attach whatever isn't worth a dedicated column.
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
    joined_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Soft-delist marker, NULL = listed. Set when a registration batch
    -- from this public_key omits a previously-registered agent_id (see
    -- repo.register_agents), or cleared again if it reappears in a later
    -- batch. Never hard-deleted — threads/thread_history still reference
    -- agent_id, and the audit trail should survive de-listing.
    delisted_at   TIMESTAMPTZ,
    UNIQUE (public_key, name)
);

CREATE TABLE IF NOT EXISTS threads (
    thread_id         TEXT PRIMARY KEY DEFAULT souk_new_id('thread'),
    agent_id          TEXT NOT NULL REFERENCES agents(agent_id),
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
    -- Only actually generated here for a fresh 'run_status' row
    -- (repo.create_run omits it from that INSERT's column list); a
    -- 'message' row belonging to an existing run passes its run_id
    -- explicitly, which bypasses this default entirely.
    run_id        TEXT NOT NULL DEFAULT souk_new_id('run'),
    kind          TEXT NOT NULL CHECK (kind IN ('message', 'run_status')),

    -- kind = 'message'
    -- Generated here, unconditionally — repo.append_thread_messages
    -- never accepts a caller-supplied id for this column, and a
    -- 'run_status' row's INSERT must explicitly set this NULL (not omit
    -- it) to avoid picking up this default by accident.
    message_id    TEXT DEFAULT souk_new_id('msg'),
    message_json  JSONB,

    -- kind = 'run_status'
    agent_id      TEXT,
    protocol      TEXT CHECK (protocol IN ('ag-ui', 'a2a')),
    -- 'input-required': the run is paused/resumable instead of
    -- finished — a provider signaled this via AG-UI's own native
    -- interrupt outcome (see souk/pause.py) rather than completing
    -- normally. Not terminal like the other non-active statuses below:
    -- resuming a paused run reopens this *same* row under its existing
    -- run_id for another round (see repo.reopen_run) rather
    -- than creating a new one, so a run's identity stays stable across
    -- however many pause/resume rounds it goes through. Excluded from
    -- the stall sweep (souk.health) as long as it's genuinely paused
    -- (not stalled mid-round), and from being re-triggered by a
    -- duplicate call on the same thread (see
    -- repo.get_active_run_for_thread).
    --
    -- 'resumed' is accepted here but no longer written by souk — kept
    -- only so this CHECK constraint doesn't reject any pre-existing
    -- rows from before pause/resume rounds were tracked this way.
    status        TEXT CHECK (status IN ('queued', 'running', 'input-required', 'resumed', 'completed', 'failed', 'cancelled')),
    input_json    JSONB,
    started_at    TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ,
    -- Bumped on every status change and every event relayed for this run
    -- (see repo.mark_run_status / repo.touch_run_activity) — how souk
    -- tells "provider claimed this and is making progress" apart from
    -- "provider claimed this and then went silent" (see the periodic
    -- stall sweep in souk.health).
    last_activity_at TIMESTAMPTZ,

    -- Free-form extension data, meaning depends on `kind`: for a message
    -- row, whatever metadata the AG-UI Message carried; for a run_status
    -- row, an A2A Task/Message's own `metadata` field when the caller was
    -- an A2A client (real A2A Tasks and Messages have one).
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (thread_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_thread_history_thread ON thread_history (thread_id, id);
-- A2A's Task.id is just this run_id (see api_a2a._start_run) — no
-- separate task_id concept, so this same unique index is what both
-- souk's own dispatch and A2A's tasks/get/tasks/cancel lookups rely on;
-- there's nothing else to index.
CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_history_run_status_run_id
    ON thread_history (run_id) WHERE kind = 'run_status';

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
