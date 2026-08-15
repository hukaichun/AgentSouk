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

`Souk` is also the domain surface an embedding caller uses — attaching an
agent, starting a run, asking what a thread or run currently looks like —
so nothing outside needs to reach into `repo` or `broker` directly.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from souk import repo
from souk.broker import Claim, Run, RunBroker, drain_run, request_cancel
from souk.config import CoreSettings
from souk.db_schema import DEFAULT_DB_SCHEMA, quoted_schema
from souk.errors import AgentNotFound, InvalidRegistration
from souk.handlers import make_handlers
from souk.identity import (
    is_timestamp_fresh,
    issue_session_token,
    registration_signing_payload,
    verify_signature,
)
from souk.kyok import KyokBridge
from souk.providers import AgentProvider

logger = logging.getLogger("souk.core")


@dataclass
class Registration:
    """What a provider gets back for proving who it is."""

    agent_ids: dict[str, str]
    session_token: str


@dataclass
class RunHandle:
    """A started run, addressable three different ways at once.

    A bare event iterator would only cover the first: AG-UI and A2A's
    `tasks/sendSubscribe` stream events as they arrive, but `tasks/send`
    drains the whole run and answers with one object, and `tasks/get` /
    `tasks/cancel` come back to a run long after the call that started it —
    A2A's `Task.id` *is* `run_id`, so the id has to be available without
    consuming (or even starting) a stream.
    """

    run_id: str
    thread_id: str
    # False when there is nothing live to consume: the run was already
    # paused or finished, or failed immediately because its agent was
    # offline. There is no in-memory run to drain in that case and the
    # answer has to be reconstructed from persisted state — callers branch
    # on this, and they branch *differently* (collecting stored events vs
    # emitting a single status update), so it is exposed rather than
    # papered over.
    is_live: bool
    _run: Run | None = None

    async def events(self) -> AsyncIterator[Any]:
        """The run's events as they arrive. Empty for a run that isn't
        live — read its persisted events (see `Souk.get_run_events`)
        instead of waiting on a stream that will never produce anything.

        Leaving this early (a caller disconnecting, breaking out of the
        loop) does not cancel the run; see `broker.drain_run`.
        """
        if self._run is None:
            return
        async for item in drain_run(self._run):
            yield item

    def cancel(self) -> None:
        """Stop the run. Synchronous on purpose — the flag flips
        immediately so nothing hands the run out in the meantime, while the
        multi-step part (DB write, telling the agent) happens in order on
        the run's own task. See `broker.request_cancel`."""
        if self._run is not None:
            request_cancel(self._run)


class Souk:
    """One configured souk. Construct with explicit settings, or with none
    to resolve them from the `SOUK_*` environment variables:

        souk = Souk()                                    # all from env
        souk = Souk(CoreSettings(database_url="..."))    # explicit
    """

    def __init__(self, settings: CoreSettings | None = None, broker: RunBroker | None = None) -> None:
        self.settings = settings or CoreSettings()
        self.engine = _create_engine(self.settings)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)
        # Live dispatch state, held per instance rather than as a module
        # singleton — the same reasoning as settings and the engine above.
        # Accepting one here is also what would let a distributed
        # implementation (Postgres SKIP LOCKED, Redis) substitute without
        # any caller changing; see docs/library-architecture.md on
        # horizontal scaling. Nothing distributed exists today.
        self.broker = broker or RunBroker(spawn=self.spawn)
        # KYOK's completion relay — structurally a second broker (see
        # souk/kyok.py), so it is held the same way for the same reasons.
        self.kyok_bridge = KyokBridge()
        # Agents running in this process, by agent_id (see attach_provider).
        self._providers: dict[str, AgentProvider] = {}
        self._heartbeat: asyncio.Task | None = None
        # Every background task this souk started — see spawn().
        self._tasks: set[asyncio.Task] = set()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A database session scoped to a block — the direct replacement for
        `async with SessionLocal() as session:`."""
        async with self.sessionmaker() as session:
            yield session

    # ---- Background work

    def spawn(self, coro, *, name: str | None = None) -> asyncio.Task:
        """Start a background task this souk owns.

        Two things this fixes over a bare `asyncio.create_task`. The loop
        keeps only a weak reference to a running task, so one nothing else
        holds can be garbage-collected mid-flight — not hypothetical, it is
        what silently killed run pipelines once already (see broker.py).
        And a fire-and-forget task has no owner at shutdown, so in-flight
        runs were simply abandoned, left for the next process start to clean
        up as orphans.

        Deliberately a supervised set rather than an `asyncio.TaskGroup`:
        a TaskGroup cancels every sibling when one task fails, and runs must
        be isolated from each other — one agent blowing up cannot be allowed
        to take down every other run in flight.
        """
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def aclose(self) -> None:
        """Stop everything this souk started, then release the database pool.

        Cancels in-flight background work and waits for it to unwind, so
        handlers get to finish their current statement rather than being
        killed mid-write. Runs still live at that point stay 'running' in the
        database and are reconciled on the next start (repo.fail_orphaned_runs)
        — souk's dispatch state is in-memory by design and does not survive a
        restart.
        """
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        await self.engine.dispose()

    # ---- Agents

    async def register_agents(
        self,
        sdk_client_id: str,
        public_key: str,
        signature: str,
        timestamp: int,
        agents: list[dict[str, Any]],
        provider_name: str | None = None,
    ) -> Registration:
        """Prove an identity holds its key, then record what it offers.

        Domain, not HTTP: the same act whether the provider is across a
        network or in this process. A provider's identity *is* its Ed25519
        keypair, and the signature is what ties this batch to it; the
        timestamp bounds how long an observed-but-valid signature could be
        replayed for.
        """
        if not is_timestamp_fresh(timestamp):
            raise InvalidRegistration("registration timestamp too far from souk's clock")
        payload = registration_signing_payload(
            sdk_client_id, [a["name"] for a in agents], timestamp
        )
        if not verify_signature(public_key, signature, payload):
            raise InvalidRegistration("invalid registration signature")

        async with self.session() as session:
            agent_ids = await repo.register_agents(
                session, sdk_client_id, public_key, agents, provider_name=provider_name
            )
        return Registration(
            agent_ids=agent_ids,
            session_token=issue_session_token(sdk_client_id, self.settings.token_signing_secret),
        )

    async def attach_provider(self, agent_id: str, provider: AgentProvider) -> None:
        """Run an agent in this process.

        Deliberately *not* a shortcut past registration. An in-process
        provider proves who it is exactly like a remote one — by having
        registered (see register_agents), which is why this refuses an
        agent_id souk has never issued. Sharing a process with souk is not
        a reason to be trusted.

        Liveness works the same way too. A remote provider says "I'm still
        here" by polling; an attached one is kept fresh by souk's own
        heartbeat, on the same last_seen_at that the roster and the
        offline fast-fail read. That matters in both directions: an
        attached provider shows as genuinely online, and if this process
        wedges the heartbeat stops and it correctly stops looking available.

        `provider` only has to have the AG-UI agent shape; see
        souk/providers.py. There is no wrapper class to construct.
        """
        if await self.get_agent(agent_id) is None:
            raise AgentNotFound(
                f"agent '{agent_id}' is not registered — a provider must register "
                "before it can be attached, in-process or not"
            )
        self._providers[agent_id] = provider
        await self._touch_attached()
        if self._heartbeat is None or self._heartbeat.done():
            self._heartbeat = self.spawn(self._heartbeat_forever(), name="provider-heartbeat")

    async def detach_provider(self, agent_id: str) -> None:
        """This provider is gone. Unlike a remote one — whose absence souk
        can only infer once it stops polling — this is a departure souk
        actually witnessed, so the agent is marked offline immediately
        rather than left to age out of the window."""
        if self._providers.pop(agent_id, None) is None:
            return
        async with self.session() as session:
            await repo.mark_agent_offline(session, agent_id, self.settings.online_window_seconds)

    async def _touch_attached(self) -> None:
        if not self._providers:
            return
        async with self.session() as session:
            for agent_id in list(self._providers):
                await repo.touch_agent(session, agent_id)

    async def _heartbeat_forever(self) -> None:
        """Keeps attached agents' last_seen_at fresh, the same signal a
        remote provider refreshes by polling. Half the online window, so a
        single missed beat never makes a live agent look offline."""
        interval = max(1, self.settings.online_window_seconds // 2)
        while True:
            await asyncio.sleep(interval)
            try:
                await self._touch_attached()
            except Exception:
                logger.exception("provider heartbeat failed")

    async def list_agents(self) -> list[dict[str, Any]]:
        """The roster, with this souk's own online/staleness policy applied."""
        async with self.session() as session:
            return await repo.list_agents(
                session,
                online_window_seconds=self.settings.online_window_seconds,
                stale_hidden_window_seconds=self.settings.stale_hidden_window_seconds,
            )

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        async with self.session() as session:
            return await repo.get_agent_by_id(session, agent_id)

    async def resolve_agents_by_name(self, name: str) -> list[dict[str, Any]]:
        """Every currently-listed agent under this display name — zero, one,
        or several, since a name is not exclusive across identities."""
        async with self.session() as session:
            return await repo.resolve_agents_by_name(session, name)

    # ---- Threads

    async def create_thread(
        self, agent_id: str, parent_thread_id: str | None = None, metadata: dict | None = None
    ) -> str:
        async with self.session() as session:
            thread_id = await repo.create_thread(session, agent_id, parent_thread_id, metadata)
            await session.commit()
            return thread_id

    async def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        async with self.session() as session:
            return await repo.get_thread(session, thread_id)

    async def get_thread_messages(self, thread_id: str) -> list[dict[str, Any]]:
        async with self.session() as session:
            return await repo.get_thread_messages(session, thread_id)

    async def get_thread_snapshot(self, thread_id: str) -> dict[str, Any] | None:
        """Messages plus the current active run — what a caller needs to
        catch up on a thread without a live stream."""
        async with self.session() as session:
            return await repo.get_thread_snapshot(session, thread_id)

    async def get_thread_tree(self, thread_id: str) -> dict[str, Any] | None:
        """Full call-chain lineage rooted at `thread_id` — itself plus every
        descendant thread spawned from it. Only as complete as callers chose
        to make it: a hop appears only if the caller recorded the lineage
        when it called through souk.
        """
        async with self.session() as session:
            root = await repo.get_thread(session, thread_id)
            if root is None:
                return None

            async def build(node_thread_id: str) -> list[dict[str, Any]]:
                children = await repo.get_thread_children(session, node_thread_id)
                return [
                    {**child, "children": await build(child["thread_id"])} for child in children
                ]

            return {
                "thread_id": thread_id,
                "agent_id": root["agent_id"],
                "children": await build(thread_id),
            }

    # ---- Runs

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        async with self.session() as session:
            return await repo.get_run(session, run_id)

    async def get_run_events(self, run_id: str, since_seq: int = 0) -> list[dict[str, Any]]:
        async with self.session() as session:
            return await repo.get_run_events(session, run_id, since_seq=since_seq)

    def active_runs(self) -> list[str]:
        """run_ids this souk is currently dispatching, from live in-memory
        state — distinct from the database's view, which also holds runs
        that already finished."""
        return self.broker.active_run_ids()

    def enqueue_run(
        self,
        run_id: str,
        agent_id: str,
        thread_id: str,
        input_json: dict[str, Any],
        protocol: str,
        seq: int = 0,
    ) -> Run:
        """Put a persisted run into live dispatch.

        The one place a run enters the broker, so an attached in-process
        provider picks up work no matter which path created the run — a
        library call, an AG-UI request, or an A2A one. A remote agent's runs
        simply sit here until it polls for them.
        """
        run = self.broker.enqueue_run(
            run_id, agent_id, thread_id, input_json, protocol, make_handlers(self), seq=seq
        )
        provider = self._providers.get(agent_id)
        if provider is not None:
            run.in_queue.put_nowait(Claim(provider))
        return run

    async def start_run(
        self,
        agent_id: str,
        run_input: dict[str, Any],
        thread_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RunHandle:
        """Start a run against `agent_id` and hand back a handle to it.

        The library entry point: persists the run, puts it into dispatch, and
        returns without waiting for it. `run_input` is an AG-UI RunAgentInput
        payload; its threadId/runId are filled in with the real ids souk
        assigns, since those are souk's to mint (a caller-supplied one is
        never trusted as a real identity).

        Protocol surfaces do more than this on the way in — persisting the
        caller's messages, verifying an actor chain, failing fast against an
        offline agent — which is theirs to do, not this method's.
        """
        async with self.session() as session:
            resolved_thread_id = await repo.ensure_thread(
                session, agent_id, thread_id, metadata=metadata, create_if_missing=True
            )
            created = await repo.create_run(session, resolved_thread_id, agent_id, "ag-ui", run_input, metadata)
            run_id = created["run_id"]

        run = self.enqueue_run(
            run_id,
            agent_id,
            resolved_thread_id,
            {**run_input, "threadId": resolved_thread_id, "runId": run_id},
            "ag-ui",
        )
        return RunHandle(run_id=run_id, thread_id=resolved_thread_id, is_live=True, _run=run)

    async def resume_run(self, run_id: str, run_input: dict[str, Any], metadata: dict | None = None) -> RunHandle:
        """Restart a paused ('input-required') run for another round under
        its *same* run_id — a run's identity stays stable across however many
        pause/resume rounds it goes through, so a caller's task id keeps
        pointing at the same task for its whole life.
        """
        async with self.session() as session:
            stored = await repo.get_run(session, run_id)
            if stored is None:
                raise LookupError(f"no such run: {run_id}")
            await repo.reopen_run(session, run_id, run_input, metadata)
            # Continue this run's existing seq rather than restarting at 0 —
            # earlier rounds already wrote events under the same run_id.
            starting_seq = await repo.get_last_event_seq(session, run_id)

        run = self.enqueue_run(
            run_id,
            stored["agent_id"],
            stored["thread_id"],
            run_input,
            stored["protocol"] or "ag-ui",
            seq=starting_seq,
        )
        return RunHandle(run_id=run_id, thread_id=stored["thread_id"], is_live=True, _run=run)

    def cancel_run(self, run_id: str) -> bool:
        """Cancel a live run. False if souk isn't dispatching it — already
        finished, or never started here."""
        run = self.broker.get(run_id)
        if run is None:
            return False
        request_cancel(run)
        return True


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
