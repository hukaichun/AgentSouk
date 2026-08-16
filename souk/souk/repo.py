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

from sqlalchemy import delete, func, insert, inspect, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from souk.identity import provider_fingerprint
from souk.ids import new_id
from souk.models import AgentRecord, AgentRef, AgentSummary, RunRecord
from souk.schema import agents, providers, run_events, runs, thread_messages, threads


# The statuses that mean a run is not finished with. Defined once because
# three questions ask it — whether a thread already has a run in flight,
# whether an agent can be deleted, and what a sweep may reap — and they must
# not drift apart. `input-required` is in here and is the one a plausible
# shorter list omits: that run is paused on a human who is coming back.
ACTIVE_RUN_STATUSES = ["queued", "running", "cancelling", "input-required"]


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


class ProviderFingerprintTaken(Exception):
    """A different key already holds this key's fingerprint — its short
    address (see souk.identity.provider_fingerprint). Refused rather than
    allowed, because two identities sharing an address is the one thing an
    address may not do. Raised from the UNIQUE constraint rather than from a
    check, so two colliding registrations arriving together cannot both pass.
    """


class RunRowMissing(Exception):
    """souk tried to move a run the database does not have.

    Not a caller error: souk only ever updates runs it is itself dispatching,
    so this means its in-memory state and the database have diverged — the row
    deleted underneath it, or the whole database replaced while this process
    kept running.
    """


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


async def get_schema_revision(session: AsyncSession) -> str | None:
    """Which migration this database is at, or None if it has never been
    migrated.

    Asks whether `alembic_version` exists rather than running the query and
    reading the failure. Catching the error would have caught more than the
    missing table: a connection failure raises OperationalError, which is a
    DBAPIError too, so an unreachable database would have reported itself as
    merely unmigrated — measured, not imagined.
    """
    connection = await session.connection()
    if not await connection.run_sync(lambda c: inspect(c).has_table("alembic_version")):
        return None
    return (
        await session.execute(select(text("version_num")).select_from(text("alembic_version")))
    ).scalars().first()


async def ensure_provider(session: AsyncSession, public_key: str) -> None:
    """Record this identity, so it has a row whether or not it ever names
    itself — that row is what a short address resolves through (see
    souk/schema.py's providers table).

    Raises `ProviderFingerprintTaken` if another key already holds this
    key's fingerprint. The check is the UNIQUE index doing it, not a
    read-then-write here: two colliding registrations arriving together
    would both pass a check and one would still have to lose, and only the
    database can decide that without a race.
    """
    now = _utcnow()
    stmt = _upsert(session, providers).values(
        public_key=public_key,
        fingerprint=provider_fingerprint(public_key),
        updated_at=now,
    )
    # Nothing to update: both columns here are derived from the key, so a
    # second registration by the same identity has nothing new to say.
    stmt = stmt.on_conflict_do_nothing(index_elements=[providers.c.public_key])
    try:
        await session.execute(stmt)
    except IntegrityError as e:
        await session.rollback()
        raise ProviderFingerprintTaken(
            f"another provider already holds fingerprint {provider_fingerprint(public_key)}"
        ) from e


async def set_provider_name(session: AsyncSession, public_key: str, display_name: str) -> None:
    """Sets this identity's storefront label — see souk/schema.py's providers
    table notes. Only called when a registration batch actually includes
    `provider_name`; register_agents leaves any existing label untouched
    otherwise (a registration that doesn't happen to pass one isn't "no
    name", it's "didn't say"). The row itself is ensured separately, since
    an identity exists whether or not it is named.
    """
    await session.execute(
        update(providers)
        .where(providers.c.public_key == public_key)
        .values(display_name=display_name, updated_at=_utcnow())
    )


async def register_agents(
    session: AsyncSession,
    public_key: str,
    agents_batch: list[dict[str, Any]],
    provider_name: str | None = None,
) -> dict[str, AgentRef]:
    """Upserts this batch under `public_key`, and marks anything previously
    registered by that key but absent from it **offline** — not gone.

    Absence used to de-list, on the reasoning that a batch is the declarative
    full statement of what an identity offers, which made re-registration the
    whole de-listing UX with no separate endpoint needed. That is a
    convenience argument, and it bought the convenience with a failure of
    exactly issue #37's shape: silent and indistinguishable from healthy. A
    provider that starts with a partial list — a config error, a flag off,
    half a deploy, a loop over the wrong collection — de-listed everything it
    failed to mention, logged nothing, and went on claiming for those agents
    successfully, because ownership never consulted `delisted_at` either.

    Removing an agent is `delete_agent`, which cannot be reached by accident.
    That is the whole argument.

    `name` is not exclusive — a different public_key may freely reuse the
    same name (see the UNIQUE(public_key, name) constraint in souk/schema.py).
    A name that went quiet and comes back in a later batch is simply seen
    again.

    Returns this batch's `AgentRef`s indexed by name — the pairs, which the
    caller already knew, rather than ids souk minted for it to hold. Handing
    ids back is what made a provider's vocabulary depend on which database
    answered (see docs/retiring-agent-id.md).
    """
    await ensure_provider(session, public_key)
    if provider_name is not None:
        await set_provider_name(session, public_key, provider_name)

    now = _utcnow()
    registered: dict[str, AgentRef] = {}
    for agent in agents_batch:
        name = agent["name"]
        card = {
            "name": name,
            "description": agent.get("description", ""),
            **agent.get("agent_card_extra", {}),
        }
        stmt = _upsert(session, agents).values(
            name=name,
            provider_key=public_key,
            agent_card=card,
            metadata=agent.get("metadata", {}),
            joined_at=now,
            last_seen_at=now,
        )
        # joined_at is deliberately left out of the update set — an existing
        # row keeps its original join time.
        stmt = stmt.on_conflict_do_update(
            index_elements=[agents.c.provider_key, agents.c.name],
            set_={
                "agent_card": stmt.excluded.agent_card,
                "metadata": stmt.excluded.metadata,
                "last_seen_at": now,
            },
        )
        await session.execute(stmt)
        registered[name] = AgentRef(provider_key=public_key, name=name)

    await session.commit()
    return registered


async def get_agent_names_for_provider(session: AsyncSession, provider_key: str) -> set[str]:
    """Which names this key has registered — what stops one provider being
    attached for another's agents (see Souk.attach_provider).

    Names, because within one provider a name *is* unique
    (`PRIMARY KEY (provider_key, name)`), and the key is already known: it
    comes from the provider itself, which holds the private half. souk-agent-sdk built a routing
    table on the belief that "name is no longer a unique routing key", which
    was the right observation at the wrong scope — names are not unique across
    providers, and inside one they always were.
    """
    rows = (
        await session.execute(
            select(agents.c.name).where(agents.c.provider_key == provider_key)
        )
    ).scalars().all()
    return set(rows)


async def touch_agents(session: AsyncSession, provider_key: str, names: list[str]) -> None:
    """Marks this provider's agents as seen — one statement for the batch,
    where it used to be one per agent."""
    if not names:
        return
    await session.execute(
        update(agents)
        .where(agents.c.provider_key == provider_key, agents.c.name.in_(names))
        .values(last_seen_at=_utcnow())
    )
    await session.commit()


async def get_agent(session: AsyncSession, agent: AgentRef) -> AgentRecord | None:
    """Direct, always-unambiguous lookup by the identity itself. An agent
    exists or it does not — there is no third state, since absence from a
    registration batch marks it offline rather than hiding it (see
    register_agents) and removing one is `delete_agent`."""
    row = (
        await session.execute(
            select(
                agents.c.provider_key,
                agents.c.name,
                agents.c.agent_card,
                agents.c.metadata,
                agents.c.joined_at,
                agents.c.last_seen_at,
            ).where(
                agents.c.provider_key == agent.provider_key,
                agents.c.name == agent.name,
            )
        )
    ).mappings().first()
    return AgentRecord(**row) if row else None


async def resolve_agent(session: AsyncSession, provider: str, name: str) -> dict[str, Any] | None:
    """The agent this identity registered under this name, or None.

    `provider` is the identity's public key or its fingerprint — the two are
    different lengths (64 hex against 16), so one parameter takes either
    without ambiguity and neither can be mistaken for the other.

    Addressing an agent by *whose* it is and what they called it, which is
    what an agent's identity has been all along: `UNIQUE(public_key, name)`
    is the primary key itself. So unlike resolve_agents_by_name this can never be
    ambiguous — the pair is either registered or it is not — and callers
    have nothing to disambiguate and no 409 to surface.

    """
    row = (
        await session.execute(
            select(
                agents.c.provider_key,
                agents.c.name,
                agents.c.agent_card,
                agents.c.metadata,
                agents.c.joined_at,
                agents.c.last_seen_at,
            )
            # Outer, so addressing by the full key still works for an agent
            # whose identity somehow has no providers row.
            .select_from(agents.outerjoin(providers, providers.c.public_key == agents.c.provider_key))
            .where(
                or_(agents.c.provider_key == provider, providers.c.fingerprint == provider),
                agents.c.name == name,
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
                agents.c.provider_key,
                agents.c.name,
                agents.c.agent_card,
                agents.c.metadata,
                agents.c.joined_at,
                agents.c.last_seen_at,
            )
            .where(agents.c.name == name)
            .order_by(agents.c.joined_at)
        )
    ).mappings().all()
    return [dict(row) for row in rows]


async def list_agents(
    session: AsyncSession,
    *,
    stale_hidden_window_seconds: int,
) -> list[AgentSummary]:
    """The stored half of the roster. `online` is left False here and filled
    in by `Souk.list_agents`, because whether an agent is reachable is not a
    fact this table holds — see `RunBroker.serving`.

    `last_seen_at` still decides what is *hidden*: an agent nothing has heard
    from in a week drops off the roster entirely rather than sitting there
    offline forever. That is a different question from reachability and it is
    one the database can answer.
    """
    stale_cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_hidden_window_seconds)
    rows = (
        await session.execute(
            select(
                agents.c.provider_key,
                agents.c.name,
                agents.c.agent_card,
                agents.c.joined_at,
                agents.c.last_seen_at,
                providers.c.display_name.label("provider_name"),
            )
            .select_from(
                agents.outerjoin(providers, providers.c.public_key == agents.c.provider_key)
            )
            .where(agents.c.last_seen_at >= stale_cutoff)
            .order_by(agents.c.name)
        )
    ).mappings().all()
    return [
        AgentSummary(
            provider_key=row["provider_key"],
            name=row["name"],
            description=row["agent_card"].get("description", ""),
            skills=row["agent_card"].get("skills", []),
            joined_at=row["joined_at"],
            last_seen_at=row["last_seen_at"],
            provider_name=row["provider_name"],
        )
        for row in rows
    ]


async def count_threads_for_agent(session: AsyncSession, agent: AgentRef) -> int:
    """How many conversations this agent has. Zero is what makes it
    deletable (see Souk.delete_agent).

    Threads are sufficient on their own: a run always lives in a thread owned
    by the same agent (`ensure_thread` raises otherwise) and a message lives
    in a run's thread, so no history of any kind can exist for an agent with
    no threads. `delete_agent` checks the runs too — the sentence above is an
    invariant, and invariants are what quietly stop being true.
    """
    return (
        await session.execute(
            select(func.count())
            .select_from(threads)
            .where(
                threads.c.provider_key == agent.provider_key,
                threads.c.agent_name == agent.name,
            )
        )
    ).scalar_one()


async def count_runs_for_agent(session: AsyncSession, agent: AgentRef, statuses: list[str] | None = None) -> int:
    """Runs for this agent, optionally only those in `statuses`."""
    where = [runs.c.provider_key == agent.provider_key, runs.c.agent_name == agent.name]
    if statuses is not None:
        where.append(runs.c.status.in_(statuses))
    return (
        await session.execute(select(func.count()).select_from(runs).where(*where))
    ).scalar_one()


async def delete_agent(session: AsyncSession, agent: AgentRef) -> bool:
    """Removes the row. One statement, no cascade — everything that would
    need cascading is what `Souk.delete_agent` refuses to delete over.

    False if there was nothing to remove, which the caller has already
    checked for; it is returned rather than raised because a second delete of
    the same agent is not an error, it is a no-op someone repeated.
    """
    result = await session.execute(
        delete(agents).where(
            agents.c.provider_key == agent.provider_key, agents.c.name == agent.name
        )
    )
    await session.commit()
    return result.rowcount > 0


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
            .where(agents.c.provider_key == public_key)
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
    agent: AgentRef,
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
            provider_key=agent.provider_key,
            agent_name=agent.name,
            parent_thread_id=parent_thread_id,
            metadata=metadata or {},
            created_at=now,
            last_activity_at=now,
        )
    )
    return thread_id


async def ensure_thread(
    session: AsyncSession,
    agent: AgentRef,
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
       the agent addressed — ThreadOwnershipMismatch otherwise, never silently
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
                return await create_thread(session, agent, metadata=metadata)
            raise ThreadNotFound(thread_id)
        owner = AgentRef(
            provider_key=existing["provider_key"], name=existing["agent_name"]
        )
        if owner != agent:
            raise ThreadOwnershipMismatch(
                f"thread '{thread_id}' belongs to agent '{owner}', not '{agent}'"
            )
        await session.execute(
            update(threads).where(threads.c.thread_id == thread_id).values(last_activity_at=_utcnow())
        )
        return thread_id

    return await create_thread(session, agent, parent_thread_id, metadata)


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
            select(
                threads.c.thread_id,
                threads.c.provider_key,
                threads.c.agent_name,
                threads.c.created_at,
            )
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
            insert(thread_messages).values(
                thread_id=thread_id,
                run_id=run_id,
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
            select(thread_messages.c.message_json)
            .where(thread_messages.c.thread_id == thread_id)
            .order_by(thread_messages.c.id)
        )
    ).all()
    return [row.message_json for row in rows]


async def create_run(
    session: AsyncSession,
    thread_id: str,
    agent: AgentRef,
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
        insert(runs).values(
            run_id=run_id,
            thread_id=thread_id,
            provider_key=agent.provider_key,
            agent_name=agent.name,
            protocol=protocol,
            status="queued",
            input_json=input_json,
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
            select(runs.c.metadata).where(runs.c.run_id == run_id)
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

    Sets status back to 'queued' so a claim can hand it out again;
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
        update(runs).where(runs.c.run_id == run_id).values(**values)
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
    # Every status change counts as activity (see runs.last_activity_at).
    values: dict[str, Any] = {"status": status, "last_activity_at": now}
    if timestamp_col:
        values[timestamp_col] = now
    if metadata:
        values["metadata"] = await _merge_run_metadata(session, run_id, metadata)
    result = await session.execute(
        update(runs).where(runs.c.run_id == str(run_id)).values(**values)
    )
    await session.commit()
    # Nothing updated means souk is dispatching a run this database has never
    # heard of — its row deleted underneath it, or the database itself
    # replaced while a process kept running. The rowcount was previously
    # discarded, so that produced a run which reached a verdict, told its
    # caller a whole story, and left no trace: measured by wiping souk's
    # tables mid-run, which the run survived without a single complaint.
    #
    # Raised rather than logged. The caller here is always souk's own run
    # pipeline (see handlers), which catches it, logs it, and still terminates
    # the run's stream — so the run ends visibly instead of continuing to
    # write into nothing.
    if result.rowcount == 0:
        raise RunRowMissing(
            f"run {run_id}: no such run in the database — souk is dispatching a run "
            "this database does not have"
        )


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
            select(runs)
            .where(
                runs.c.thread_id == thread_id,
                runs.c.status.in_(ACTIVE_RUN_STATUSES),
            )
            .order_by(runs.c.created_at.desc())
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
    a worker reporting events — so a run that's producing output
    doesn't look stalled even without a status change.
    """
    await session.execute(
        update(runs).where(runs.c.run_id == run_id).values(last_activity_at=_utcnow())
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
            select(runs.c.run_id, runs.c.metadata).where(where_clause)
        )
    ).all()
    now = _utcnow()
    run_ids: list[str] = []
    for row in rows:
        await session.execute(
            update(runs)
            .where(runs.c.run_id == row.run_id)
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
    or completed again (claiming only ever consults the broker, not the
    DB). Mark them 'failed' so the DB stops claiming they're still live.

    Deliberately narrow: the WHERE clause only ever touches rows still in
    a non-terminal state. Runs already 'completed'/'failed'/'cancelled'
    are untouched — every run can fail, but a run that already finished
    keeps the state it finished in, always.
    """
    return await _fail_runs(
        session,
        runs.c.status.in_(["queued", "running", "cancelling"]),
        "orphaned_by_souk_restart",
    )


async def fail_stalled_runs(session: AsyncSession, stall_timeout_seconds: int) -> list[str]:
    """Called periodically (see souk.health) while souk is live. A run
    only ever reaches 'running' once a provider has acked it — if it then
    goes this long without any activity (no further event, see
    touch_run_activity), the provider took it and went silent: a real
    anomaly, distinct from a run still sitting 'queued', which nobody has
    taken and which the broker gives up on itself (see
    RunBroker.expire_queued).

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
        runs.c.status.in_(["running", "cancelling"]) & (runs.c.last_activity_at < cutoff),
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
        (runs.c.status == "input-required") & (runs.c.last_activity_at < cutoff),
        "paused_no_resume",
    )


async def get_run(session: AsyncSession, run_id: str) -> RunRecord | None:
    """`select(runs)` — and that is safe again, which it was not while runs
    shared `thread_history` with messages. Selecting the whole row there also
    returned `id`, `kind`, `message_id` and `message_json`: the columns that
    made the sharing work, handed back as if they were facts about a run. The
    fix then was to name every column; the fix now is that the table holds a
    run and nothing else, so its columns and `models.RunRecord`'s fields are
    the same set by construction rather than by two lists agreeing.
    """
    row = (
        await session.execute(select(runs).where(runs.c.run_id == run_id))
    ).mappings().first()
    return RunRecord(**row) if row else None


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
