"""Database access helpers, written against SQLAlchemy Core's expression
language over the tables in souk/schema.py. Two handfuls of tables and
simple queries don't need a full ORM, but going through Core (rather than
the raw `text()` SQL this module used to hold) is what lets the same code
run on both SQLite and Postgres: Core renders dialect-correct SQL and, just
as importantly, returns the same Python types on either backend — dict for
JSON columns, datetime for timestamps.

A few things that used to be Postgres-side now live here in Python, for the
same portability reason:

- Entity ids are minted with souk.ids.new_id and passed explicitly on
  insert (the old `souk_new_id()` DB function had no SQLite equivalent).
  Because the id is known before the insert, no `RETURNING` round-trip is
  needed to learn it.
- Timestamps are `datetime.now(timezone.utc)` bound as parameters, not a
  SQL `now()` / `make_interval()` — one consistent UTC source across
  dialects (see souk/schema.py's note on timestamp storage).
- JSON metadata merges (the old jsonb `||` operator) are done as a shallow
  Python dict merge after reading the current value.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from souk.ids import new_id
from souk.schema import agents, providers, run_events, thread_history, threads


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _upsert(session: AsyncSession, table):
    """A dialect-appropriate INSERT construct that supports ON CONFLICT.

    Both the Postgres and SQLite `insert()` constructs expose the same
    `.on_conflict_do_update()` / `.excluded` API; only the factory differs.
    Taken from the session's own bind rather than a module-level engine, so
    this module has no import-time dependency on a configured engine and two
    Souks on different backends can coexist in one process.
    """
    is_postgres = session.bind.dialect.name == "postgresql"
    return (pg_insert if is_postgres else sqlite_insert)(table)


class ThreadNotFound(Exception):
    """Raised by ensure_thread when a caller-supplied thread_id doesn't
    exist — a caller error (referencing an id souk never issued, or one
    it made up itself), not a request to create one under that name; see
    create_thread's docstring for why there's no implicit-creation
    fallback for this.
    """


class ThreadOwnershipMismatch(Exception):
    """Raised by ensure_thread when a thread_id resolves to a real
    thread, but one owned by a different agent than the caller is
    addressing right now.
    """


async def upsert_provider_name(session: AsyncSession, public_key: str, display_name: str) -> None:
    """Sets/updates this public_key's storefront label — see
    souk/schema.py's providers table notes. Only called when a registration
    batch actually includes `provider_name`; register_agents leaves any
    existing label untouched otherwise (a registration that doesn't
    happen to pass one isn't "no name", it's "didn't say").
    """
    now = _utcnow()
    stmt = _upsert(session, providers).values(public_key=public_key, display_name=display_name, updated_at=now)
    stmt = stmt.on_conflict_do_update(
        index_elements=[providers.c.public_key],
        set_={"display_name": stmt.excluded.display_name, "updated_at": now},
    )
    await session.execute(stmt)


async def register_agents(
    session: AsyncSession,
    public_key: str,
    agents_batch: list[dict[str, Any]],
    provider_name: str | None = None,
) -> dict[str, str]:
    """Upserts this batch under `public_key`, then de-lists (soft-delete)
    anything previously owned by this same `public_key` that's absent from
    it — the batch is treated as the declarative full statement of "what
    this identity currently offers" (see the module-level design notes in
    the project plan: this is what makes a plain re-registration call the
    entire de-listing UX, no separate endpoint needed).

    `name` is not exclusive — a different public_key may freely reuse the
    same name (see the UNIQUE(public_key, name) constraint in souk/schema.py).
    An agent_id is assigned once per (public_key, name) pair and reused on
    every subsequent registration of that same pair; a name reappearing
    after being de-listed clears delisted_at again (self-heal).

    Returns {name: agent_id} for this batch.
    """
    if provider_name is not None:
        await upsert_provider_name(session, public_key, provider_name)

    names = [agent["name"] for agent in agents_batch]
    existing = (
        await session.execute(
            select(agents.c.agent_id, agents.c.name).where(
                agents.c.public_key == public_key, agents.c.name.in_(names)
            )
        )
    ).all()
    existing_ids = {row.name: row.agent_id for row in existing}

    now = _utcnow()
    agent_ids: dict[str, str] = {}
    for agent in agents_batch:
        name = agent["name"]
        card = {
            "name": name,
            "description": agent.get("description", ""),
            **agent.get("agent_card_extra", {}),
        }
        # Reuse the existing id for an already-registered (public_key, name)
        # pair so the upsert lands on that same row; mint a fresh one for a
        # genuinely new pair. Either way the id is known here in Python —
        # the database never generates it.
        agent_id = existing_ids.get(name) or new_id("agent")
        stmt = _upsert(session, agents).values(
            agent_id=agent_id,
            name=name,
            public_key=public_key,
            agent_card=card,
            metadata=agent.get("metadata", {}),
            joined_at=now,
            last_seen_at=now,
        )
        # joined_at is deliberately left out of the update set — an existing
        # row keeps its original join time; only a re-list clears delisted_at.
        stmt = stmt.on_conflict_do_update(
            index_elements=[agents.c.agent_id],
            set_={
                "agent_card": stmt.excluded.agent_card,
                "metadata": stmt.excluded.metadata,
                "last_seen_at": now,
                "delisted_at": None,
            },
        )
        await session.execute(stmt)
        agent_ids[name] = agent_id

    await session.execute(
        update(agents)
        .where(
            agents.c.public_key == public_key,
            agents.c.delisted_at.is_(None),
            agents.c.agent_id.notin_(list(agent_ids.values())),
        )
        .values(delisted_at=now)
    )
    await session.commit()
    return agent_ids


async def get_agent_ids_for_public_key(session: AsyncSession, public_key: str) -> set[str]:
    """Which agent_ids this key actually owns — what stops a valid token for
    one provider being used to claim another's work (see Souk.claim_work).

    By public_key because that is what ownership *is* here: agent_id is
    assigned per (public_key, name) and de-listing sweeps by public_key. This
    used to filter on a self-declared `sdk_client_id` instead, which two
    unrelated keypairs could pick the same value for — and then each could
    claim the other's runs, measured, not theorised.
    """
    rows = (
        await session.execute(select(agents.c.agent_id).where(agents.c.public_key == public_key))
    ).scalars().all()
    return set(rows)


async def touch_agent(session: AsyncSession, agent_id: str) -> None:
    await session.execute(
        update(agents).where(agents.c.agent_id == agent_id).values(last_seen_at=_utcnow())
    )
    await session.commit()


async def mark_agent_offline(session: AsyncSession, agent_id: str, online_window_seconds: int) -> None:
    """Backdate last_seen_at past the online window, so an agent whose
    provider has genuinely gone shows as offline immediately instead of
    lingering until the window expires. Used when a provider detaches — a
    departure souk actually witnessed, unlike a remote provider that simply
    stops polling and has to be inferred.
    """
    await session.execute(
        update(agents)
        .where(agents.c.agent_id == agent_id)
        .values(last_seen_at=_utcnow() - timedelta(seconds=online_window_seconds + 1))
    )
    await session.commit()


async def get_agent_by_id(session: AsyncSession, agent_id: str) -> dict[str, Any] | None:
    """Direct, always-unambiguous lookup by the canonical key — a delisted
    agent is treated as not found, same as one that never existed."""
    row = (
        await session.execute(
            select(
                agents.c.agent_id,
                agents.c.name,
                agents.c.agent_card,
                agents.c.metadata,
                agents.c.joined_at,
                agents.c.last_seen_at,
            ).where(agents.c.agent_id == agent_id, agents.c.delisted_at.is_(None))
        )
    ).mappings().first()
    return dict(row) if row else None


async def get_agent_public_key(session: AsyncSession, agent_id: str) -> str | None:
    """Just the identity half of get_agent_by_id — see souk.protocols.kyok.
    chat_completions, which needs this to verify a KyokSigningAuth
    signature (souk_agent_sdk.kyok_auth) against the actual key this
    agent_id registered with, without pulling the whole agent_card/
    metadata row it doesn't need for that. A delisted agent has no usable
    key here either, same as get_agent_by_id.
    """
    return (
        await session.execute(
            select(agents.c.public_key).where(
                agents.c.agent_id == agent_id, agents.c.delisted_at.is_(None)
            )
        )
    ).scalars().first()


async def resolve_agent(session: AsyncSession, public_key: str, name: str) -> dict[str, Any] | None:
    """The agent this identity registered under this name, or None.

    Addressing an agent by *whose* it is and what they called it, which is
    what an agent's identity has been all along: `UNIQUE(public_key, name)`
    is the natural key an agent_id is assigned per, and de-listing sweeps by
    public_key. So unlike resolve_agents_by_name this can never be
    ambiguous — the pair is either registered or it is not — and callers
    have nothing to disambiguate and no 409 to surface.

    A delisted agent is not found, same as get_agent_by_id.
    """
    row = (
        await session.execute(
            select(
                agents.c.agent_id,
                agents.c.name,
                agents.c.public_key,
                agents.c.agent_card,
                agents.c.metadata,
                agents.c.joined_at,
                agents.c.last_seen_at,
            ).where(
                agents.c.public_key == public_key,
                agents.c.name == name,
                agents.c.delisted_at.is_(None),
            )
        )
    ).mappings().first()
    return dict(row) if row else None


async def resolve_agents_by_name(session: AsyncSession, name: str) -> list[dict[str, Any]]:
    """Every currently-listed agent registered under this display name —
    zero, one (the common case), or many if multiple identities picked the
    same name. Callers of the legacy `/a2a/{name}/...`/`/agui/{name}`
    routes use this to either transparently resolve (exactly one match) or
    surface a 409 with the candidate list (more than one) — see api_a2a.py/
    api_agui.py.
    """
    rows = (
        await session.execute(
            select(
                agents.c.agent_id,
                agents.c.name,
                agents.c.public_key,
                agents.c.agent_card,
                agents.c.metadata,
                agents.c.joined_at,
                agents.c.last_seen_at,
            )
            .where(agents.c.name == name, agents.c.delisted_at.is_(None))
            .order_by(agents.c.joined_at)
        )
    ).mappings().all()
    return [dict(row) for row in rows]


def is_agent_online(last_seen_at: datetime, online_window_seconds: int) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=online_window_seconds)
    return last_seen_at.replace(tzinfo=timezone.utc) >= cutoff


async def list_agents(
    session: AsyncSession,
    *,
    online_window_seconds: int,
    stale_hidden_window_seconds: int,
) -> list[dict[str, Any]]:
    stale_cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_hidden_window_seconds)
    rows = (
        await session.execute(
            select(
                agents.c.agent_id,
                agents.c.name,
                agents.c.agent_card,
                agents.c.joined_at,
                agents.c.last_seen_at,
                agents.c.public_key,
                providers.c.display_name.label("provider_name"),
            )
            .select_from(
                agents.outerjoin(providers, providers.c.public_key == agents.c.public_key)
            )
            .where(agents.c.delisted_at.is_(None), agents.c.last_seen_at >= stale_cutoff)
            .order_by(agents.c.name)
        )
    ).mappings().all()
    return [
        {
            "agent_id": row["agent_id"],
            "name": row["name"],
            "description": row["agent_card"].get("description", ""),
            "skills": row["agent_card"].get("skills", []),
            "joined_at": row["joined_at"],
            "last_seen_at": row["last_seen_at"],
            "online": is_agent_online(row["last_seen_at"], online_window_seconds),
            "public_key": row["public_key"],
            "provider_name": row["provider_name"],
        }
        for row in rows
    ]


async def get_agent_name_for_public_key(session: AsyncSession, public_key: str) -> str | None:
    """Resolves a verified caller's public key (see souk.identity's
    a2a_call_signing_payload / protocols.a2a's _start_run) back to one of its
    registered agent names, purely for audit/display — if the key owns
    several names this just picks one, since the point is establishing
    "this is a known, registered identity", not which specific name.
    """
    return (
        await session.execute(
            select(agents.c.name)
            .where(agents.c.public_key == public_key)
            .order_by(agents.c.joined_at)
            .limit(1)
        )
    ).scalars().first()


async def get_thread(session: AsyncSession, thread_id: str) -> dict[str, Any] | None:
    row = (
        await session.execute(select(threads).where(threads.c.thread_id == thread_id))
    ).mappings().first()
    return dict(row) if row else None


async def create_thread(
    session: AsyncSession,
    agent_id: str,
    parent_thread_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Always mints a genuinely fresh thread_id (souk.ids.new_id('thread'))
    and stores it. Callers: the optional `POST /threads` endpoint
    (Souk.create_thread), and `ensure_thread` itself — both its
    `parent_thread_id` case (no existing child thread for that pairing yet)
    and its no-id-at-all/unrecognized-id fallback (see that function's
    docstring and souk-no-forced-protocol-deviation: a standard AG-UI/A2A
    caller that never called `POST /threads` must still get a real thread
    minted for it on first contact, not a 404).
    """
    thread_id = new_id("thread")
    now = _utcnow()
    await session.execute(
        insert(threads).values(
            thread_id=thread_id,
            agent_id=agent_id,
            parent_thread_id=parent_thread_id,
            metadata=metadata or {},
            created_at=now,
            last_activity_at=now,
        )
    )
    return thread_id


async def ensure_thread(
    session: AsyncSession,
    agent_id: str,
    thread_id: str | None,
    parent_thread_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    create_if_missing: bool = False,
) -> str:
    """Returns the thread_id to use for a run — always succeeds, one way
    or another (see souk-no-forced-protocol-deviation: souk must not
    force callers into an explicit `POST /threads` step the standard
    protocol they're speaking doesn't require).

    1. `thread_id` given and it already exists: use it (must belong to
       `agent_id` — ThreadOwnershipMismatch otherwise, never silently
       reassigned).
    2. `thread_id` given but unrecognized:
       - `create_if_missing=True` (api_agui.py — AG-UI's `threadId` is a
         required field the *caller* mints locally; an unrecognized one
         is indistinguishable from "this is a brand new conversation",
         since AG-UI has no separate "create thread" concept for a
         caller to have used instead): silently mint a fresh, real
         (database-generated) thread_id and use that — the caller's own
         string is discarded, same as any other caller-supplied id (see
         append_thread_messages) — and learns the real one back the
         standard AG-UI way: the run's own RUN_STARTED event.
       - `create_if_missing=False` (api_a2a.py's default — A2A's
         `contextId` is optional; a caller that supplies one is claiming
         to continue something specific): ThreadNotFound — a caller
         error, not a request to create one under that name.
    3. `thread_id` is None (e.g. A2A's `tasks/send` with no `contextId`
       at all): always mint a fresh thread — the normal, spec-sanctioned
       first-contact case (A2A: "Agents MAY generate a new contextId
       when processing a Message that does not include one"), not an
       error. This holds even when `parent_thread_id` is given (a
       sub-agent call carrying `Message.referenceTaskIds` — see
       protocols.a2a's _start_run): lineage recording and session continuity are
       deliberately orthogonal (A2A's own `referenceTaskIds` is
       explicitly informational-only, not a session-grouping primitive
       — see souk-no-forced-protocol-deviation). souk still stores
       `parent_thread_id` on the fresh thread so lineage stays complete
       (see get_thread_children), but never reuses an existing child
       thread just because the same parent referenced it before — a
       caller that wants to continue talking to the same callee thread
       must say so explicitly, the standard A2A way: pass back the real
       `contextId` it was returned on the earlier call.
    """
    if thread_id is not None:
        existing = await get_thread(session, thread_id)
        if existing is None:
            if create_if_missing:
                return await create_thread(session, agent_id, metadata=metadata)
            raise ThreadNotFound(thread_id)
        if existing["agent_id"] != agent_id:
            raise ThreadOwnershipMismatch(
                f"thread '{thread_id}' belongs to agent '{existing['agent_id']}', not '{agent_id}'"
            )
        await session.execute(
            update(threads).where(threads.c.thread_id == thread_id).values(last_activity_at=_utcnow())
        )
        return thread_id

    return await create_thread(session, agent_id, parent_thread_id, metadata)


async def get_thread_children(session: AsyncSession, thread_id: str) -> list[dict[str, Any]]:
    """Direct children of `thread_id` (see threads.parent_thread_id) —
    walked recursively by the caller (Souk.get_thread_tree) to build a
    full lineage tree. souk is the one party that actually sees every A2A
    hop, so this is data it can own outright; it only gets populated when
    a caller sets Message.referenceTaskIds though (real A2A, see
    protocols.a2a's _start_run), so it's only as complete as callers choose to
    make it.
    """
    rows = (
        await session.execute(
            select(threads.c.thread_id, threads.c.agent_id, threads.c.created_at)
            .where(threads.c.parent_thread_id == thread_id)
            .order_by(threads.c.created_at)
        )
    ).mappings().all()
    return [dict(row) for row in rows]


async def append_thread_messages(
    session: AsyncSession, thread_id: str, run_id: str, messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Stores each message under a souk-minted message_id
    (souk.ids.new_id('msg')) — any `id` a message already carried
    (caller-supplied or otherwise) is discarded, never trusted as this
    row's real identity. Returns the same messages with `id` overwritten to
    the id that's now authoritative in the database, since callers
    (api_agui.py/api_a2a.py) hand this exact return value to
    build_run_agent_input — the provider must see the same id souk itself
    now uses for this message, not whatever it looked like before this call.

    Because the id is minted here in Python, the final message (id already
    embedded) is stored in a single insert — no second update to backfill a
    database-generated id, as the old raw-SQL version needed.
    """
    stored: list[dict[str, Any]] = []
    for message in messages:
        message_id = new_id("msg")
        final_message = {**message, "id": message_id}
        await session.execute(
            insert(thread_history).values(
                thread_id=thread_id,
                run_id=run_id,
                kind="message",
                message_id=message_id,
                message_json=final_message,
                metadata=message.get("metadata", {}),
            )
        )
        stored.append(final_message)
    return stored


async def get_thread_messages(session: AsyncSession, thread_id: str) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(thread_history.c.message_json)
            .where(thread_history.c.thread_id == thread_id, thread_history.c.kind == "message")
            .order_by(thread_history.c.id)
        )
    ).all()
    return [row.message_json for row in rows]


async def create_run(
    session: AsyncSession,
    thread_id: str,
    agent_id: str,
    protocol: str,
    input_json: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> dict[str, str]:
    # run_id is minted in Python (souk.ids.new_id('run')). message_id is set
    # explicitly to NULL — that column carries a real id only on 'message'
    # rows, and a 'run_status' row must not pick one up. A2A's Task.id is
    # just this run_id (see protocols.a2a's _start_run) — no separate task_id.
    run_id = new_id("run")
    await session.execute(
        insert(thread_history).values(
            thread_id=thread_id,
            kind="run_status",
            run_id=run_id,
            agent_id=agent_id,
            protocol=protocol,
            status="queued",
            input_json=input_json,
            message_id=None,
            metadata=metadata or {},
            last_activity_at=_utcnow(),
        )
    )
    await session.commit()
    return {"run_id": run_id}


async def _merge_run_metadata(
    session: AsyncSession, run_id: str, metadata: dict[str, Any]
) -> dict[str, Any]:
    """Shallow-merge `metadata` into a run_status row's existing metadata,
    returning the merged dict to store — the portable stand-in for the old
    jsonb `||` operator (which is shallow too). Reads the current value
    first; a missing row yields `{}` as the base, same as `NULL || x`
    would have produced nothing to merge onto.
    """
    existing = (
        await session.execute(
            select(thread_history.c.metadata).where(
                thread_history.c.run_id == run_id, thread_history.c.kind == "run_status"
            )
        )
    ).scalars().first()
    return {**(existing or {}), **metadata}


async def reopen_run(
    session: AsyncSession, run_id: str, input_json: dict[str, Any], metadata: dict[str, Any] | None = None
) -> None:
    """Restarts a paused ('input-required') run for another round under
    its *same* run_id, instead of minting a new one via create_run — see
    souk/pause.py's module docstring: a stable identity across however
    many pause/resume rounds a run goes through (HITL approval) is what
    lets a caller's A2A Task.id (== this run_id, see protocols.a2a's _start_run)
    keep pointing at the same task for its whole life (see api_a2a.py's
    tasks/get, tasks/cancel) instead of needing to chase a resume chain.

    Sets status back to 'queued' so PollForWork can hand it out again;
    deliberately does not touch started_at (this run's *first* claim is
    still when it truly started, not this round) or completed_at (this
    round isn't done either — mirrors mark_run_status's 'input-required'
    handling, just in the other direction).
    """
    values: dict[str, Any] = {
        "status": "queued",
        "input_json": input_json,
        "last_activity_at": _utcnow(),
    }
    if metadata:
        values["metadata"] = await _merge_run_metadata(session, run_id, metadata)
    await session.execute(
        update(thread_history)
        .where(thread_history.c.run_id == run_id, thread_history.c.kind == "run_status")
        .values(**values)
    )
    await session.commit()


async def mark_run_status(
    session: AsyncSession, run_id: str, status: str, metadata: dict[str, Any] | None = None
) -> None:
    """`metadata`, if given, is merged (shallow) into the run's existing
    metadata rather than replacing it — used to attach pause details (see
    souk/pause.py) when status='input-required'.

    'input-required' deliberately has no completed_at: it isn't done, it's
    paused — see the CHECK constraint's comment in souk/schema.py.
    """
    timestamp_col = {
        "running": "started_at",
        "completed": "completed_at",
        "failed": "completed_at",
        "cancelled": "completed_at",
    }.get(status)
    now = _utcnow()
    # Every status change counts as activity (see thread_history.last_activity_at).
    values: dict[str, Any] = {"status": status, "last_activity_at": now}
    if timestamp_col:
        values[timestamp_col] = now
    if metadata:
        values["metadata"] = await _merge_run_metadata(session, run_id, metadata)
    await session.execute(
        update(thread_history)
        .where(thread_history.c.run_id == str(run_id), thread_history.c.kind == "run_status")
        .values(**values)
    )
    await session.commit()


async def get_active_run_for_thread(session: AsyncSession, thread_id: str) -> dict[str, Any] | None:
    """The thread's run that's still 'open' in some sense — not yet
    completed/failed/cancelled. Used to enforce a single active run per
    thread: while one exists, a new call on the same thread must not
    start a second, concurrent one (see protocols.agui's AGUIAdapter.run /
    protocols.a2a's _start_run) — that would fork the thread's otherwise linear
    history with no clean way to merge it back.
    """
    row = (
        await session.execute(
            select(thread_history)
            .where(
                thread_history.c.thread_id == thread_id,
                thread_history.c.kind == "run_status",
                thread_history.c.status.in_(["queued", "running", "cancelling", "input-required"]),
            )
            .order_by(thread_history.c.id.desc())
            .limit(1)
        )
    ).mappings().first()
    return dict(row) if row else None


async def get_thread_snapshot(session: AsyncSession, thread_id: str) -> dict[str, Any] | None:
    """Everything a caller needs to catch up on a thread without a live
    stream: accumulated messages plus the current active run (if any).
    Used both by GET /threads/{thread_id} (for a caller reconnecting after
    its original SSE closed — e.g. once a run it was watching paused) and
    to answer a duplicate call on a thread that already has an active run,
    instead of starting a second one.
    """
    thread = await get_thread(session, thread_id)
    if thread is None:
        return None
    messages = await get_thread_messages(session, thread_id)
    active_run = await get_active_run_for_thread(session, thread_id)
    return {"thread_id": thread_id, "messages": messages, "active_run": active_run}


async def touch_run_activity(session: AsyncSession, run_id: str) -> None:
    """Called whenever an event is relayed for a run — see
    souk_server's AgentSession relay — so a run that's producing output
    doesn't look stalled even without a status change.
    """
    await session.execute(
        update(thread_history)
        .where(thread_history.c.run_id == run_id, thread_history.c.kind == "run_status")
        .values(last_activity_at=_utcnow())
    )


async def _fail_runs(
    session: AsyncSession, where_clause, failure_reason: str
) -> list[str]:
    """Shared body of the run-reaping sweeps below: find every run_status
    row matching `where_clause`, mark it 'failed' with completed_at=now and
    `failureReason` merged into its metadata, and return the run_ids
    touched. Reading-then-updating per row (rather than one bulk UPDATE
    with a jsonb merge and RETURNING) is the portable equivalent — these
    sweeps are periodic and low-volume, so the extra statements are cheap.
    """
    rows = (
        await session.execute(
            select(thread_history.c.id, thread_history.c.run_id, thread_history.c.metadata).where(
                where_clause
            )
        )
    ).all()
    now = _utcnow()
    run_ids: list[str] = []
    for row in rows:
        await session.execute(
            update(thread_history)
            .where(thread_history.c.id == row.id)
            .values(
                status="failed",
                completed_at=now,
                metadata={**(row.metadata or {}), "failureReason": failure_reason},
            )
        )
        run_ids.append(row.run_id)
    await session.commit()
    return run_ids


async def fail_orphaned_runs(session: AsyncSession) -> list[str]:
    """Called once on souk startup. souk's live dispatch state (souk.broker)
    is pure in-memory — a restart loses it entirely, so any run still
    'queued' or 'running' in the DB at that point will never be picked up
    or completed again (PollForWork only ever consults the broker, not the
    DB). Mark them 'failed' so the DB stops claiming they're still live.

    Deliberately narrow: the WHERE clause only ever touches rows still in
    a non-terminal state. Runs already 'completed'/'failed'/'cancelled'
    are untouched — every run can fail, but a run that already finished
    keeps the state it finished in, always.
    """
    return await _fail_runs(
        session,
        (thread_history.c.kind == "run_status")
        & (thread_history.c.status.in_(["queued", "running", "cancelling"])),
        "orphaned_by_souk_restart",
    )


async def fail_stalled_runs(session: AsyncSession, stall_timeout_seconds: int) -> list[str]:
    """Called periodically (see souk.health) while souk is live. A run
    only ever reaches 'running' once a provider has explicitly claimed
    it — if it then goes this long without any activity (no further
    event, see touch_run_activity), the provider claimed it and went
    silent: a real anomaly, distinct from a run merely sitting 'queued'
    waiting to be claimed (see PollRequest.max_claim — a provider
    throttling itself is expected, not a failure).

    Same narrowness guarantee as fail_orphaned_runs: only rows still
    'running' past the timeout are touched; everything else (including
    runs that produced fresh activity moments ago) is left alone. A
    status meaning "paused, waiting on something outside souk" is
    excluded here the same way terminal statuses already are — see
    fail_stale_paused_runs for that status's own, separately-configured
    sweep.
    """
    cutoff = _utcnow() - timedelta(seconds=stall_timeout_seconds)
    return await _fail_runs(
        session,
        (thread_history.c.kind == "run_status")
        & (thread_history.c.status.in_(["running", "cancelling"]))
        & (thread_history.c.last_activity_at < cutoff),
        "stalled_no_activity",
    )


async def fail_stale_paused_runs(session: AsyncSession, timeout_seconds: int) -> list[str]:
    """Called periodically (see souk.health) only when
    settings.paused_timeout_seconds is set — a run left 'input-required'
    (see souk.pause) is waiting on a human, not a provider, so unlike
    fail_stalled_runs there's no generally-correct default duration for
    this; it's opt-in per deployment.

    Same shape as fail_stalled_runs, just against 'input-required'
    instead of 'running'.
    """
    cutoff = _utcnow() - timedelta(seconds=timeout_seconds)
    return await _fail_runs(
        session,
        (thread_history.c.kind == "run_status")
        & (thread_history.c.status == "input-required")
        & (thread_history.c.last_activity_at < cutoff),
        "paused_no_resume",
    )


async def fail_unclaimed_runs(
    session: AsyncSession, timeout_seconds: int, *, online_window_seconds: int
) -> list[str]:
    """Distinct from fail_stalled_runs: catches a run that's sat 'queued'
    (never claimed at all) past `timeout_seconds' *and* whose target agent
    is no longer online — the race case where a provider was online (or
    ambiguously so) when the call was made, got queued, then went dark
    before ever polling for it. The common "target already known offline
    at call time" case is handled synchronously instead (see
    protocols.a2a's _start_run/protocols.agui's AGUIAdapter.run's fast-fail path) — this sweep is
    only the fallback for the race, not the primary mechanism, so it's
    deliberately not firing on every provider that's simply throttling
    itself via PollRequest.max_claim (see fail_stalled_runs's docstring on
    why 'queued' alone isn't a health signal).
    """
    created_cutoff = _utcnow() - timedelta(seconds=timeout_seconds)
    online_cutoff = _utcnow() - timedelta(seconds=online_window_seconds)
    # The old raw SQL used Postgres's UPDATE ... FROM agents; the portable
    # form is to find the matching run_status rows via a join, then fail
    # them (see _fail_runs) — so the join lives in this SELECT instead.
    rows = (
        await session.execute(
            select(thread_history.c.id, thread_history.c.run_id, thread_history.c.metadata)
            .select_from(
                thread_history.join(agents, agents.c.agent_id == thread_history.c.agent_id)
            )
            .where(
                thread_history.c.kind == "run_status",
                thread_history.c.status == "queued",
                thread_history.c.created_at < created_cutoff,
                agents.c.last_seen_at < online_cutoff,
            )
        )
    ).all()
    now = _utcnow()
    run_ids: list[str] = []
    for row in rows:
        await session.execute(
            update(thread_history)
            .where(thread_history.c.id == row.id)
            .values(
                status="failed",
                completed_at=now,
                metadata={**(row.metadata or {}), "failureReason": "no_provider_online"},
            )
        )
        run_ids.append(row.run_id)
    await session.commit()
    return run_ids


async def get_run(session: AsyncSession, run_id: str) -> dict[str, Any] | None:
    row = (
        await session.execute(
            select(thread_history).where(
                thread_history.c.run_id == run_id, thread_history.c.kind == "run_status"
            )
        )
    ).mappings().first()
    return dict(row) if row else None


async def append_run_event(session: AsyncSession, run_id: str, seq: int, event_json: dict[str, Any]) -> None:
    await session.execute(
        insert(run_events).values(run_id=run_id, seq=seq, event_json=event_json, created_at=_utcnow())
    )
    await session.commit()


async def get_run_events(session: AsyncSession, run_id: str, since_seq: int = 0) -> list[dict[str, Any]]:
    """`since_seq` (exclusive) restricts this to one pause/resume round's
    own events — see broker.Run.round_starting_seq's docstring for why
    that matters to handlers._handle_finish.
    """
    rows = (
        await session.execute(
            select(run_events.c.event_json)
            .where(run_events.c.run_id == run_id, run_events.c.seq > since_seq)
            .order_by(run_events.c.seq)
        )
    ).all()
    return [row.event_json for row in rows]


async def get_last_event_seq(session: AsyncSession, run_id: str) -> int:
    """The highest seq already persisted for this run_id, or 0 if none
    yet. Needed when reopening a run for another round (see
    repo.reopen_run) — the in-memory broker.Run object driving that
    round is a brand new object (the previous round's pipeline already
    terminated), so its seq counter would otherwise restart at 0 and
    collide with run_events rows this same run_id already wrote in an
    earlier round — see broker.RunBroker.enqueue_run's `seq` parameter.
    """
    return (
        await session.execute(
            select(func.coalesce(func.max(run_events.c.seq), 0)).where(run_events.c.run_id == run_id)
        )
    ).scalar()
