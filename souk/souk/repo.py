"""Postgres access helpers, kept as plain SQL via SQLAlchemy Core — two
handfuls of tables and simple queries don't need a full ORM or Alembic for
v1 (souk/db.py bootstraps the schema with idempotent CREATE TABLE IF NOT
EXISTS on startup instead).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from souk.config import settings


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
    souk/db.py's providers table notes. Only called when a registration
    batch actually includes `provider_name`; register_agents leaves any
    existing label untouched otherwise (a registration that doesn't
    happen to pass one isn't "no name", it's "didn't say").
    """
    await session.execute(
        text(
            """
            INSERT INTO providers (public_key, display_name, updated_at)
            VALUES (:public_key, :display_name, now())
            ON CONFLICT (public_key) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                updated_at = now()
            """
        ),
        {"public_key": public_key, "display_name": display_name},
    )


async def register_agents(
    session: AsyncSession,
    sdk_client_id: str,
    public_key: str,
    agents: list[dict[str, Any]],
    provider_name: str | None = None,
) -> dict[str, str]:
    """Upserts this batch under `public_key`, then de-lists (soft-delete)
    anything previously owned by this same `public_key` that's absent from
    it — the batch is treated as the declarative full statement of "what
    this identity currently offers" (see the module-level design notes in
    the project plan: this is what makes a plain re-registration call the
    entire de-listing UX, no separate endpoint needed).

    `name` is not exclusive — a different public_key may freely reuse the
    same name (see the UNIQUE(public_key, name) constraint in souk/db.py).
    An agent_id is assigned once per (public_key, name) pair and reused on
    every subsequent registration of that same pair; a name reappearing
    after being de-listed clears delisted_at again (self-heal).

    Returns {name: agent_id} for this batch.
    """
    if provider_name is not None:
        await upsert_provider_name(session, public_key, provider_name)

    names = [agent["name"] for agent in agents]
    existing = (
        await session.execute(
            text(
                "SELECT agent_id, name FROM agents WHERE public_key = :public_key AND name = ANY(:names)"
            ),
            {"public_key": public_key, "names": names},
        )
    ).mappings().all()
    existing_ids = {row["name"]: row["agent_id"] for row in existing}

    agent_ids: dict[str, str] = {}
    for agent in agents:
        name = agent["name"]
        card = {
            "name": name,
            "description": agent.get("description", ""),
            **agent.get("agent_card_extra", {}),
        }
        # `:agent_id` is the *existing* id when this (public_key, name)
        # pair has already registered before — reused explicitly so the
        # UPSERT below lands on that same row. For a genuinely new pair
        # it's NULL, and the database itself generates the real id
        # (souk_new_id('agent')) — never precomputed in Python.
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO agents
                        (agent_id, name, sdk_client_id, public_key, agent_card, metadata, joined_at, last_seen_at)
                    VALUES
                        (COALESCE(:agent_id, souk_new_id('agent')), :name, :sdk_client_id, :public_key,
                         :agent_card, :metadata, now(), now())
                    ON CONFLICT (agent_id) DO UPDATE SET
                        sdk_client_id = EXCLUDED.sdk_client_id,
                        agent_card = EXCLUDED.agent_card,
                        metadata = EXCLUDED.metadata,
                        last_seen_at = now(),
                        delisted_at = NULL
                    RETURNING agent_id
                    """
                ),
                {
                    "agent_id": existing_ids.get(name),
                    "name": name,
                    "sdk_client_id": sdk_client_id,
                    "public_key": public_key,
                    "agent_card": json.dumps(card),
                    "metadata": json.dumps(agent.get("metadata", {})),
                },
            )
        ).mappings().first()
        agent_ids[name] = row["agent_id"]

    await session.execute(
        text(
            """
            UPDATE agents SET delisted_at = now()
            WHERE public_key = :public_key AND delisted_at IS NULL
              AND NOT (agent_id = ANY(:agent_ids))
            """
        ),
        {"public_key": public_key, "agent_ids": list(agent_ids.values())},
    )
    await session.commit()
    return agent_ids


async def get_agent_ids_for_sdk_client(session: AsyncSession, sdk_client_id: str) -> set[str]:
    """Which agent_ids this token's holder actually owns — used to stop a
    valid token for one provider being used to poll for another provider's
    agent ids in souk.grpc_server.PollForWork.
    """
    rows = (
        await session.execute(
            text("SELECT agent_id FROM agents WHERE sdk_client_id = :sdk_client_id"),
            {"sdk_client_id": sdk_client_id},
        )
    ).scalars().all()
    return set(rows)


async def touch_agent(session: AsyncSession, agent_id: str) -> None:
    await session.execute(
        text("UPDATE agents SET last_seen_at = now() WHERE agent_id = :agent_id"),
        {"agent_id": agent_id},
    )
    await session.commit()


async def get_agent_by_id(session: AsyncSession, agent_id: str) -> dict[str, Any] | None:
    """Direct, always-unambiguous lookup by the canonical key — a delisted
    agent is treated as not found, same as one that never existed."""
    row = (
        await session.execute(
            text(
                "SELECT agent_id, name, agent_card, metadata, joined_at, last_seen_at "
                "FROM agents WHERE agent_id = :agent_id AND delisted_at IS NULL"
            ),
            {"agent_id": agent_id},
        )
    ).mappings().first()
    return dict(row) if row else None


async def get_agent_public_key(session: AsyncSession, agent_id: str) -> str | None:
    """Just the identity half of get_agent_by_id — see souk.api_llm_bridge.
    chat_completions, which needs this to verify a KyokSigningAuth
    signature (souk_agent_sdk.kyok_auth) against the actual key this
    agent_id registered with, without pulling the whole agent_card/
    metadata row it doesn't need for that. A delisted agent has no usable
    key here either, same as get_agent_by_id.
    """
    row = (
        await session.execute(
            text("SELECT public_key FROM agents WHERE agent_id = :agent_id AND delisted_at IS NULL"),
            {"agent_id": agent_id},
        )
    ).mappings().first()
    return row["public_key"] if row else None


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
            text(
                "SELECT agent_id, name, public_key, agent_card, metadata, joined_at, last_seen_at "
                "FROM agents WHERE name = :name AND delisted_at IS NULL ORDER BY joined_at"
            ),
            {"name": name},
        )
    ).mappings().all()
    return [dict(row) for row in rows]


def is_agent_online(last_seen_at: datetime) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.online_window_seconds)
    return last_seen_at.replace(tzinfo=timezone.utc) >= cutoff


async def list_agents(session: AsyncSession) -> list[dict[str, Any]]:
    stale_cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.stale_hidden_window_seconds)
    rows = (
        await session.execute(
            text(
                """
                SELECT a.agent_id, a.name, a.agent_card, a.joined_at, a.last_seen_at,
                       a.public_key, p.display_name AS provider_name
                FROM agents a
                LEFT JOIN providers p ON p.public_key = a.public_key
                WHERE a.delisted_at IS NULL AND a.last_seen_at >= :stale_cutoff
                ORDER BY a.name
                """
            ),
            {"stale_cutoff": stale_cutoff},
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
            "online": is_agent_online(row["last_seen_at"]),
            "public_key": row["public_key"],
            "provider_name": row["provider_name"],
        }
        for row in rows
    ]


async def get_agent_name_for_public_key(session: AsyncSession, public_key: str) -> str | None:
    """Resolves a verified caller's public key (see souk.identity's
    a2a_call_signing_payload / api_a2a._start_run) back to one of its
    registered agent names, purely for audit/display — if the key owns
    several names this just picks one, since the point is establishing
    "this is a known, registered identity", not which specific name.
    """
    return (
        await session.execute(
            text("SELECT name FROM agents WHERE public_key = :public_key ORDER BY joined_at LIMIT 1"),
            {"public_key": public_key},
        )
    ).scalars().first()


async def get_thread(session: AsyncSession, thread_id: str) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text("SELECT * FROM threads WHERE thread_id = :thread_id"), {"thread_id": thread_id}
        )
    ).mappings().first()
    return dict(row) if row else None


async def create_thread(
    session: AsyncSession,
    agent_id: str,
    parent_thread_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Always mints a genuinely fresh thread_id (database-generated —
    see souk/db.py's threads.thread_id DEFAULT) and stores it. Callers:
    the optional `POST /threads` endpoint (api_agui.create_thread), and
    `ensure_thread` itself — both its `parent_thread_id` case (no
    existing child thread for that pairing yet) and its no-id-at-all/
    unrecognized-id fallback (see that function's docstring and
    souk-no-forced-protocol-deviation: a standard AG-UI/A2A caller that
    never called `POST /threads` must still get a real thread minted for
    it on first contact, not a 404).
    """
    row = (
        await session.execute(
            text(
                """
                INSERT INTO threads (agent_id, parent_thread_id, metadata, created_at, last_activity_at)
                VALUES (:agent_id, :parent_thread_id, :metadata, now(), now())
                RETURNING thread_id
                """
            ),
            {
                "agent_id": agent_id,
                "parent_thread_id": parent_thread_id,
                "metadata": json.dumps(metadata or {}),
            },
        )
    ).mappings().first()
    return row["thread_id"]


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
         `sessionId` is optional; a caller that supplies one is claiming
         to continue something specific): ThreadNotFound — a caller
         error, not a request to create one under that name.
    3. `thread_id` is None but `parent_thread_id` is given (a sub-agent
       call spawned from within another thread's run): reuse the
       existing child thread for this (parent_thread_id, agent_id) pair
       if one exists, so repeated delegation calls keep talking to the
       same sub-thread — otherwise create_thread's a fresh one. Always
       implicit: it's a relationship derived from the parent thread the
       delegating agent already knows, not something for it to create
       explicitly ahead of time.
    4. Neither given (e.g. A2A's `tasks/send` with no `sessionId` at
       all): mint a fresh thread — this is the normal, spec-sanctioned
       first-contact case (A2A: "Agents MAY generate a new contextId
       when processing a Message that does not include one"), not an
       error.
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
            text("UPDATE threads SET last_activity_at = now() WHERE thread_id = :thread_id"),
            {"thread_id": thread_id},
        )
        return thread_id
    elif parent_thread_id is not None:
        row = (
            await session.execute(
                text(
                    """
                    SELECT thread_id FROM threads
                    WHERE parent_thread_id = :parent_thread_id AND agent_id = :agent_id
                    ORDER BY created_at LIMIT 1
                    """
                ),
                {"parent_thread_id": parent_thread_id, "agent_id": agent_id},
            )
        ).mappings().first()
        if row is not None:
            await session.execute(
                text("UPDATE threads SET last_activity_at = now() WHERE thread_id = :thread_id"),
                {"thread_id": row["thread_id"]},
            )
            return row["thread_id"]
        return await create_thread(session, agent_id, parent_thread_id, metadata)

    return await create_thread(session, agent_id, metadata=metadata)


async def get_thread_children(session: AsyncSession, thread_id: str) -> list[dict[str, Any]]:
    """Direct children of `thread_id` (see threads.parent_thread_id) —
    walked recursively by the caller (api_agui.get_thread_tree) to build a
    full lineage tree. souk is the one party that actually sees every A2A
    hop, so this is data it can own outright; it only gets populated when
    a caller sets Message.referenceTaskIds though (real A2A, see
    api_a2a._start_run), so it's only as complete as callers choose to
    make it.
    """
    rows = (
        await session.execute(
            text(
                "SELECT thread_id, agent_id, created_at FROM threads "
                "WHERE parent_thread_id = :thread_id ORDER BY created_at"
            ),
            {"thread_id": thread_id},
        )
    ).mappings().all()
    return [dict(row) for row in rows]


async def append_thread_messages(
    session: AsyncSession, thread_id: str, run_id: str, messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Stores each message under a database-generated message_id
    (souk_new_id('msg') — see souk/db.py's thread_history.message_id
    DEFAULT) — any `id` a message already carried (caller-supplied or
    otherwise) is discarded, never trusted as this row's real identity.
    Returns the same messages with `id` overwritten to the id that's now
    authoritative in the database, since callers (api_agui.py/
    api_a2a.py) hand this exact return value to build_run_agent_input —
    the provider must see the same id souk itself now uses for this
    message, not whatever it looked like before this call.
    """
    stored: list[dict[str, Any]] = []
    for message in messages:
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO thread_history (thread_id, run_id, kind, message_json, metadata)
                    VALUES (:thread_id, :run_id, 'message', :message_json, :metadata)
                    RETURNING message_id
                    """
                ),
                {
                    "thread_id": thread_id,
                    "run_id": run_id,
                    "message_json": json.dumps(message),
                    "metadata": json.dumps(message.get("metadata", {})),
                },
            )
        ).mappings().first()
        final_message = {**message, "id": row["message_id"]}
        await session.execute(
            text(
                "UPDATE thread_history SET message_json = :message_json "
                "WHERE thread_id = :thread_id AND message_id = :message_id"
            ),
            {
                "message_json": json.dumps(final_message),
                "thread_id": thread_id,
                "message_id": row["message_id"],
            },
        )
        stored.append(final_message)
    return stored


async def get_thread_messages(session: AsyncSession, thread_id: str) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                """
                SELECT message_json FROM thread_history
                WHERE thread_id = :thread_id AND kind = 'message'
                ORDER BY id
                """
            ),
            {"thread_id": thread_id},
        )
    ).mappings().all()
    return [row["message_json"] for row in rows]


async def create_run(
    session: AsyncSession,
    thread_id: str,
    agent_id: str,
    protocol: str,
    input_json: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> dict[str, str]:
    # run_id is omitted from the INSERT below so the database generates it
    # (souk_new_id('run') — see souk/db.py's thread_history.run_id
    # DEFAULT), not Python. message_id must still be set explicitly to
    # NULL — that column also has a database default (souk_new_id('msg'),
    # for 'message'-kind rows), which a 'run_status' row must not pick up
    # by accident. A2A's Task.id is just this run_id (see
    # api_a2a._start_run) — no separate task_id to generate here.
    row = (
        await session.execute(
            text(
                """
                INSERT INTO thread_history
                    (thread_id, kind, agent_id, protocol, status, input_json, message_id, metadata, last_activity_at)
                VALUES
                    (:thread_id, 'run_status', :agent_id, :protocol, 'queued', :input_json, NULL, :metadata, now())
                RETURNING run_id
                """
            ),
            {
                "thread_id": thread_id,
                "agent_id": agent_id,
                "protocol": protocol,
                "input_json": json.dumps(input_json),
                "metadata": json.dumps(metadata or {}),
            },
        )
    ).mappings().first()
    await session.commit()
    return {"run_id": row["run_id"]}


async def reopen_run(
    session: AsyncSession, run_id: str, input_json: dict[str, Any], metadata: dict[str, Any] | None = None
) -> None:
    """Restarts a paused ('input-required') run for another round under
    its *same* run_id, instead of minting a new one via create_run — see
    souk/pause.py's module docstring: a stable identity across however
    many pause/resume rounds a run goes through (HITL approval) is what
    lets a caller's A2A Task.id (== this run_id, see api_a2a._start_run)
    keep pointing at the same task for its whole life (see api_a2a.py's
    tasks/get, tasks/cancel) instead of needing to chase a resume chain.

    Sets status back to 'queued' so PollForWork can hand it out again;
    deliberately does not touch started_at (this run's *first* claim is
    still when it truly started, not this round) or completed_at (this
    round isn't done either — mirrors mark_run_status's 'input-required'
    handling, just in the other direction).
    """
    metadata_set = ", metadata = metadata || CAST(:metadata AS jsonb)" if metadata else ""
    params: dict[str, Any] = {"run_id": run_id, "input_json": json.dumps(input_json)}
    if metadata:
        params["metadata"] = json.dumps(metadata)
    await session.execute(
        text(
            "UPDATE thread_history SET status = 'queued', input_json = :input_json, "
            f"last_activity_at = now(){metadata_set} WHERE run_id = :run_id AND kind = 'run_status'"
        ),
        params,
    )
    await session.commit()


async def mark_run_status(
    session: AsyncSession, run_id: str, status: str, metadata: dict[str, Any] | None = None
) -> None:
    """`metadata`, if given, is merged (shallow, jsonb `||`) into the run's
    existing metadata rather than replacing it — used to attach pause
    details (see souk/pause.py) when status='input-required'.

    'input-required' deliberately has no completed_at: it isn't done, it's
    paused — see the CHECK constraint's comment in souk/db.py.
    """
    timestamp_col = {
        "running": "started_at",
        "completed": "completed_at",
        "failed": "completed_at",
        "cancelled": "completed_at",
    }.get(status)
    extra_set = f", {timestamp_col} = now()" if timestamp_col else ""
    # CAST(...) rather than `:metadata::jsonb` — SQLAlchemy's bind-param
    # scanner (at least on the psycopg dialect) fails to recognize a bind
    # name immediately followed by a `::` cast and leaves it unbound.
    metadata_set = ", metadata = metadata || CAST(:metadata AS jsonb)" if metadata else ""
    params: dict[str, Any] = {"status": status, "run_id": str(run_id)}
    if metadata:
        params["metadata"] = json.dumps(metadata)
    # Every status change counts as activity (see thread_history.last_activity_at).
    await session.execute(
        text(
            f"UPDATE thread_history SET status = :status, last_activity_at = now(){extra_set}{metadata_set} "
            "WHERE run_id = :run_id AND kind = 'run_status'"
        ),
        params,
    )
    await session.commit()


async def get_active_run_for_thread(session: AsyncSession, thread_id: str) -> dict[str, Any] | None:
    """The thread's run that's still 'open' in some sense — not yet
    completed/failed/cancelled. Used to enforce a single active run per
    thread: while one exists, a new call on the same thread must not
    start a second, concurrent one (see api_agui.run_agent /
    api_a2a._start_run) — that would fork the thread's otherwise linear
    history with no clean way to merge it back.
    """
    row = (
        await session.execute(
            text(
                """
                SELECT * FROM thread_history
                WHERE thread_id = :thread_id AND kind = 'run_status'
                  AND status IN ('queued', 'running', 'input-required')
                ORDER BY id DESC LIMIT 1
                """
            ),
            {"thread_id": thread_id},
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
    souk.grpc_server._relay_event — so a run that's producing output
    doesn't look stalled even without a status change.
    """
    await session.execute(
        text(
            "UPDATE thread_history SET last_activity_at = now() "
            "WHERE run_id = :run_id AND kind = 'run_status'"
        ),
        {"run_id": run_id},
    )


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
    rows = (
        await session.execute(
            text(
                """
                UPDATE thread_history
                SET status = 'failed',
                    completed_at = now(),
                    metadata = metadata || '{"failureReason": "orphaned_by_souk_restart"}'::jsonb
                WHERE kind = 'run_status' AND status IN ('queued', 'running')
                RETURNING run_id
                """
            )
        )
    ).scalars().all()
    await session.commit()
    return list(rows)


async def fail_stalled_runs(session: AsyncSession, stall_timeout_seconds: int) -> list[str]:
    """Called periodically (see souk.health) while souk is live. A run
    only ever reaches 'running' once a provider has explicitly claimed
    it — if it then goes this long without any activity (no further
    event, see touch_run_activity), the provider claimed it and went
    silent: a real anomaly, distinct from a run merely sitting 'queued'
    waiting to be claimed (see PollRequest.max_claim — a provider
    throttling itself is expected, not a failure).

    NOTE for whoever adds resumable/HITL runs later (see
    thread_history.status's CHECK constraint): a status meaning "paused,
    waiting on something outside souk" (e.g. tool-call approval) must be
    excluded from this WHERE clause the same way terminal statuses
    already are — it would otherwise get incorrectly killed by this sweep.

    Same narrowness guarantee as fail_orphaned_runs: only rows still
    'running' past the timeout are touched; everything else (including
    runs that produced fresh activity moments ago) is left alone.
    """
    rows = (
        await session.execute(
            text(
                """
                UPDATE thread_history
                SET status = 'failed',
                    completed_at = now(),
                    metadata = metadata || '{"failureReason": "stalled_no_activity"}'::jsonb
                WHERE kind = 'run_status' AND status = 'running'
                  AND last_activity_at < now() - make_interval(secs => :timeout)
                RETURNING run_id
                """
            ),
            {"timeout": stall_timeout_seconds},
        )
    ).scalars().all()
    await session.commit()
    return list(rows)


async def fail_unclaimed_runs(session: AsyncSession, timeout_seconds: int) -> list[str]:
    """Distinct from fail_stalled_runs: catches a run that's sat 'queued'
    (never claimed at all) past `timeout_seconds' *and* whose target agent
    is no longer online — the race case where a provider was online (or
    ambiguously so) when the call was made, got queued, then went dark
    before ever polling for it. The common "target already known offline
    at call time" case is handled synchronously instead (see
    api_a2a._start_run/api_agui.run_agent's fast-fail path) — this sweep is
    only the fallback for the race, not the primary mechanism, so it's
    deliberately not firing on every provider that's simply throttling
    itself via PollRequest.max_claim (see fail_stalled_runs's docstring on
    why 'queued' alone isn't a health signal).
    """
    rows = (
        await session.execute(
            text(
                """
                UPDATE thread_history th
                SET status = 'failed',
                    completed_at = now(),
                    metadata = th.metadata || '{"failureReason": "no_provider_online"}'::jsonb
                FROM agents a
                WHERE th.kind = 'run_status' AND th.status = 'queued'
                  AND th.agent_id = a.agent_id
                  AND th.created_at < now() - make_interval(secs => :timeout)
                  AND a.last_seen_at < now() - make_interval(secs => :online_window)
                RETURNING th.run_id
                """
            ),
            {"timeout": timeout_seconds, "online_window": settings.online_window_seconds},
        )
    ).scalars().all()
    await session.commit()
    return list(rows)


async def get_run(session: AsyncSession, run_id: str) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text("SELECT * FROM thread_history WHERE run_id = :run_id AND kind = 'run_status'"),
            {"run_id": run_id},
        )
    ).mappings().first()
    return dict(row) if row else None



async def append_run_event(session: AsyncSession, run_id: str, seq: int, event_json: dict[str, Any]) -> None:
    await session.execute(
        text(
            "INSERT INTO run_events (run_id, seq, event_json, created_at) VALUES (:run_id, :seq, :event_json, now())"
        ),
        {"run_id": run_id, "seq": seq, "event_json": json.dumps(event_json)},
    )
    await session.commit()


async def get_run_events(session: AsyncSession, run_id: str, since_seq: int = 0) -> list[dict[str, Any]]:
    """`since_seq` (exclusive) restricts this to one pause/resume round's
    own events — see broker.Run.round_starting_seq's docstring for why
    that matters to grpc_server._handle_finish.
    """
    rows = (
        await session.execute(
            text(
                "SELECT event_json FROM run_events "
                "WHERE run_id = :run_id AND seq > :since_seq ORDER BY seq"
            ),
            {"run_id": run_id, "since_seq": since_seq},
        )
    ).mappings().all()
    return [row["event_json"] for row in rows]


async def get_last_event_seq(session: AsyncSession, run_id: str) -> int:
    """The highest seq already persisted for this run_id, or 0 if none
    yet. Needed when reopening a run for another round (see
    repo.reopen_run) — the in-memory broker.Run object driving that
    round is a brand new object (the previous round's pipeline already
    terminated), so its seq counter would otherwise restart at 0 and
    collide with run_events rows this same run_id already wrote in an
    earlier round — see broker.RunBroker.enqueue_run's `seq` parameter.
    """
    row = (
        await session.execute(
            text("SELECT COALESCE(MAX(seq), 0) AS max_seq FROM run_events WHERE run_id = :run_id"),
            {"run_id": run_id},
        )
    ).mappings().first()
    return row["max_seq"]
