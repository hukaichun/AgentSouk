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
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from souk import repo
from souk.broker import FinishStream, RelayEvent, RunBroker, RunSnapshot
from souk.config import CoreSettings
from souk.db_schema import DEFAULT_DB_SCHEMA, quoted_schema
from souk.errors import AgentNotFound, InvalidRegistration
from souk.handlers import make_handlers
from souk.identity import (
    is_timestamp_fresh,
    issue_session_token,
    registration_signing_payload,
    verify_session_token,
    verify_signature,
)
from souk.kyok import KyokBridge
from souk.providers import Provider
from souk.worker import ClaimedRun, Worker

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
    # The broker dispatching this run, not the run itself: a handle is
    # something a caller keeps, and keeping the live Run would put its queues
    # in the caller's hands (see broker.RunSnapshot).
    _broker: RunBroker | None = None
    # Subscribed when this handle is built, *not* when events() is first
    # awaited. A caller that starts reading late must still get everything
    # from the beginning, and a short run can finish — and be forgotten by
    # the broker — before anyone reads. Subscribing lazily silently returned
    # nothing for exactly those runs.
    _events: AsyncIterator[Any] | None = None

    async def events(self) -> AsyncIterator[Any]:
        """The run's events as they arrive, from the beginning. Empty for a
        run that isn't live — read its persisted events (see
        `Souk.get_run_events`) instead of waiting on a stream that will never
        produce anything.

        One stream per handle: this consumes the subscription taken when the
        handle was created, so calling it twice does not replay.

        Leaving it early (a caller disconnecting, breaking out of the loop)
        does not cancel the run; see `RunBroker.subscribe`.
        """
        if self._events is None:
            return
        async for item in self._events:
            yield item

    def cancel(self) -> None:
        """Stop the run. Synchronous on purpose — the flag flips
        immediately so nothing hands the run out in the meantime, while the
        multi-step part (DB write, telling the agent) happens in order on
        the run's own task. See `RunBroker.request_cancel`."""
        if self._broker is not None:
            self._broker.request_cancel(self.run_id)


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
        # Providers running in this process, by provider_id, each with the
        # worker souk drives it through (see attach_provider) — the same
        # grouping a remote SDK process has, so one provider's several
        # agents share one claim budget.
        self._workers: dict[str, Worker] = {}
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
        public_key: str,
        signature: str,
        timestamp: int,
        agents: list[dict[str, Any]],
        provider_name: str | None = None,
    ) -> Registration:
        """Prove an identity holds its key, then record what it offers.

        Domain, not HTTP: the same act whether the provider is across a
        network or in this process. A provider's identity *is* its Ed25519
        keypair — there is no other id for it, and nothing it calls itself
        that souk would take at face value — so the signature is what ties
        this batch to it, and the timestamp bounds how long an
        observed-but-valid signature could be replayed for.
        """
        if not is_timestamp_fresh(timestamp):
            raise InvalidRegistration("registration timestamp too far from souk's clock")
        payload = registration_signing_payload([a["name"] for a in agents], timestamp)
        if not verify_signature(public_key, signature, payload):
            raise InvalidRegistration("invalid registration signature")

        async with self.session() as session:
            agent_ids = await repo.register_agents(
                session, public_key, agents, provider_name=provider_name
            )
        return Registration(
            agent_ids=agent_ids,
            session_token=issue_session_token(public_key, self.settings.token_signing_secret),
        )

    async def claim_work(
        self,
        session_token: str,
        agent_ids: list[str],
        *,
        max_claim: int | None = None,
        wait_seconds: float = 0,
        on_cancel: Callable[[str], None] | None = None,
    ) -> list[ClaimedRun]:
        """A worker asking for work, and leaving with it.

        The one door into souk for every worker, in-process or remote (see
        souk/worker.py) — souk deciding this identity may run these agents is
        a domain act, so it belongs here rather than in whichever transport
        happens to carry the request. A second transport (WebSocket, say)
        implements framing and calls this; it does not get to re-derive who
        owns what.

        Each run comes back with its `run_input`, because claiming *is* the
        hand-over: there is no follow-up call in which souk delivers the
        input, and so no window in which a run has been claimed but its
        worker is still waiting to be told what to do.

        `on_cancel` is how souk later *asks* this worker to stop one of these
        runs (see broker.Run.cancel_notify). Optional: a worker that offers
        no way to be asked simply never hears about it, which changes
        nothing about how the run's outcome is decided.

        Everything security-relevant happens here:

        - the session token is verified (`InvalidRegistration` if not),
          yielding the public key it was issued to — the provider's whole
          identity,
        - requested agent_ids are filtered down to ones that key actually
          owns, because a valid token for one provider must not be usable to
          claim another's agents,
        - and the agents it does own are marked as seen, which is how any
          provider — in-process or remote — stays online at all.

        `max_claim=None` means unlimited; `0` explicitly means "no capacity
        right now" and claims nothing — distinct from None, and a reason not
        to hold the call open, since only the caller can change that.
        `wait_seconds > 0` long-polls: returns as soon as work arrives for
        one of these agents rather than after a fixed sleep.
        """
        public_key = verify_session_token(session_token, self.settings.token_signing_secret)
        if public_key is None:
            raise InvalidRegistration("missing or invalid session token")

        async with self.session() as session:
            owned = await repo.get_agent_ids_for_public_key(session, public_key)
        allowed = [agent_id for agent_id in agent_ids if agent_id in owned]
        if len(allowed) != len(agent_ids):
            logger.warning(
                "claim_work: provider %s asked for agent id(s) it does not own: %s",
                public_key,
                sorted(set(agent_ids) - owned),
            )

        runs = self.broker.claim(
            allowed, claimed_by=public_key, cancel_notify=on_cancel, max_claim=max_claim
        )
        if not runs and wait_seconds > 0 and max_claim != 0:
            event = self.broker.subscribe_wake(allowed)
            try:
                await asyncio.wait_for(event.wait(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                pass
            finally:
                self.broker.unsubscribe_wake(allowed, event)
            runs = self.broker.claim(
                allowed, claimed_by=public_key, cancel_notify=on_cancel, max_claim=max_claim
            )

        async with self.session() as session:
            for agent_id in allowed:
                await repo.touch_agent(session, agent_id)
        return [
            ClaimedRun(
                run_id=run.run_id,
                agent_id=run.agent_id,
                thread_id=run.thread_id,
                run_input=run.input_json,
            )
            for run in runs
        ]

    def report_event(self, run_id: str, event: Any, *, claimed_by: str) -> bool:
        """One AG-UI event a worker produced for a run it holds.

        The return path, and the whole reason the provider port could go: an
        event now goes from wherever it was produced straight onto the run's
        own queue, with nothing in between to route it a second time.
        Synchronous, because a worker reporting an event should never be made
        to wait on souk's persistence — the run's pipeline does that, in
        order, on its own task.

        `claimed_by` is the reporting provider's public key, checked against
        the identity that actually claimed this run. Holding an authenticated
        connection is not the same as holding *this run*: without this, any
        connected provider could push events into any run_id it could guess.
        False (and nothing recorded) if the run is unknown — finished,
        cancelled, given up on — or if it belongs to somebody else.
        """
        run = self.broker.get(run_id)
        if run is None:
            # Ordinary: a straggler from a run souk already stopped
            # dispatching, e.g. one the health sweep gave up on.
            return False
        if run.claimed_by != claimed_by:
            logger.warning(
                "report_event: '%s' reported for run %s, which is held by '%s'",
                claimed_by,
                run_id,
                run.claimed_by,
            )
            return False
        return self.broker.push(run_id, RelayEvent(event))

    def finish_run(self, run_id: str, *, claimed_by: str) -> bool:
        """The worker holding this run says its agent's stream has ended.

        The authoritative end of a run, and the only one: souk decides the
        outcome from what it saw (see handlers._handle_finish) rather than
        from anything the worker asserts about it. Same ownership check as
        `report_event` — ending someone else's run is exactly as much of a
        forgery as producing events for it.
        """
        run = self.broker.get(run_id)
        if run is None:
            return False
        if run.claimed_by != claimed_by:
            logger.warning(
                "finish_run: '%s' tried to end run %s, which is held by '%s'",
                claimed_by,
                run_id,
                run.claimed_by,
            )
            return False
        return self.broker.push(run_id, FinishStream())

    async def attach_provider(
        self,
        provider_id: str,
        provider: Provider,
        agent_ids: list[str],
        *,
        max_claim: int | None = None,
    ) -> None:
        """Run a provider in this process.

        A provider, not an agent: `provider_id` is the identity that
        registered — its Ed25519 **public key** (hex), which is the only id a
        provider has — and `agent_ids` are which of its agents it is here to
        serve. One object serving several agents is the ordinary case, not a
        special one, which is why the port hands it the `agent_id` of each
        run and lets it route (see souk/providers.py).

        Deliberately *not* a shortcut past registration, in either
        direction:

        - every agent_id must be one this key actually registered. Attaching
          used to derive the provider from the agent, which meant there was
          nothing to check — the answer was whatever the agent row said.
          Declaring the identity first is what makes it checkable, and
          `AgentNotFound` is raised for an id this key does not own.
        - the worker souk starts claims over the same `claim_work` a remote
          provider calls, with a real session token issued to that same key,
          subject to the same ownership filtering. Sharing a process with
          souk is not a reason to be trusted, and is no longer a reason to
          take a different path.

        Liveness follows from that rather than needing its own mechanism:
        claiming marks these agents seen, so an attached provider stays
        online exactly the way a remote one does, and if this process wedges
        its worker stops claiming and it correctly stops looking available.
        (An in-process heartbeat used to exist for this. It was a second
        mechanism for a fact the claim loop already produces.)

        `max_claim` is how many runs this provider will have in flight at
        once, across every agent it hosts — the in-process counterpart of
        PollRequest.max_claim. None (the default) is unlimited, matching the
        remote SDK's default.

        Attaching the same provider_id again replaces what it serves: the
        provider object, its agent list and its budget. The worker keeps
        running, and runs already in flight are untouched.
        """
        if not agent_ids:
            raise ValueError(
                f"provider '{provider_id}' attached with no agent_ids — there would be "
                "nothing for it to claim"
            )
        async with self.session() as session:
            owned = await repo.get_agent_ids_for_public_key(session, provider_id)
        unowned = [agent_id for agent_id in agent_ids if agent_id not in owned]
        if unowned:
            raise AgentNotFound(
                f"provider '{provider_id}' has not registered agent id(s) {sorted(unowned)} — "
                "a provider must register before it can be attached, in-process or not"
            )

        worker = self._workers.get(provider_id)
        if worker is None:
            worker = Worker(
                self,
                session_token=self._issue_worker_token(provider_id),
                renew_token=partial(self._issue_worker_token, provider_id),
                provider=provider,
                agent_ids=agent_ids,
                max_claim=max_claim,
            )
            self._workers[provider_id] = worker
        else:
            worker.provider = provider
            worker.agent_ids = list(agent_ids)
            worker.max_claim = max_claim
        # Online from this moment, rather than from whenever the worker's
        # loop next comes round — attaching is itself evidence it is here.
        async with self.session() as session:
            for agent_id in agent_ids:
                await repo.touch_agent(session, agent_id)
        worker.start()

    async def detach_provider(self, provider_id: str) -> None:
        """This provider is gone from this process. Unlike a remote one —
        whose absence souk can only infer once it stops claiming — this is a
        departure souk actually witnessed, so its agents are marked offline
        immediately rather than left to age out of the window.

        Runs it is already running are left to finish: they are still
        producing, and souk records no outcome it hasn't observed.
        """
        worker = self._workers.pop(provider_id, None)
        if worker is None:
            return
        worker.stop()
        async with self.session() as session:
            for agent_id in worker.agent_ids:
                await repo.mark_agent_offline(session, agent_id, self.settings.online_window_seconds)

    def _issue_worker_token(self, provider_id: str) -> str:
        """The token an in-process provider's worker claims with — issued to
        its public key, exactly like a remote provider's. souk mints it here
        rather than exempting in-process providers from carrying one, so
        `claim_work`'s ownership filtering applies to them unchanged: an
        attached provider cannot claim another's work any more than a remote
        one can."""
        return issue_session_token(provider_id, self.settings.token_signing_secret)

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
    ) -> RunSnapshot:
        """Put a persisted run into live dispatch.

        The one place a run enters the broker, no matter which path created
        it — a library call, an AG-UI request, or an A2A one. It then waits
        to be claimed, by an in-process worker or a remote one; enqueueing
        wakes any worker currently long-polling for this agent (see
        RunBroker.enqueue_run), so waiting to be claimed costs about a
        scheduling turn in-process, not a poll interval.

        This used to hand the run straight to an attached provider here,
        which is exactly why an in-process provider had no way to throttle:
        souk pushed, and nothing asked whether it had capacity.
        """
        return self.broker.enqueue_run(
            run_id, agent_id, thread_id, input_json, protocol, make_handlers(self), seq=seq
        )

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

        self.enqueue_run(
            run_id,
            agent_id,
            resolved_thread_id,
            {**run_input, "threadId": resolved_thread_id, "runId": run_id},
            "ag-ui",
        )
        return RunHandle(
            run_id=run_id,
            thread_id=resolved_thread_id,
            is_live=True,
            _broker=self.broker,
            _events=self.broker.subscribe(run_id),
        )

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

        self.enqueue_run(
            run_id,
            stored["agent_id"],
            stored["thread_id"],
            run_input,
            stored["protocol"] or "ag-ui",
            seq=starting_seq,
        )
        return RunHandle(
            run_id=run_id,
            thread_id=stored["thread_id"],
            is_live=True,
            _broker=self.broker,
            _events=self.broker.subscribe(run_id),
        )

    def cancel_run(self, run_id: str) -> bool:
        """Cancel a live run. False if souk isn't dispatching it — already
        finished, or never started here."""
        return self.broker.request_cancel(run_id)


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
