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
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from souk import repo
from souk.agui import build_run_agent_input
from souk.broker import (
    ConnectedProvider,
    FinishStream,
    RelayEvent,
    RunBroker,
    RunSnapshot,
)
from souk.changes import ChangeEvent, LlmRosterChanged, RosterChanged, RunStatusChanged
from souk.config import CoreSettings
from souk.db_schema import DEFAULT_DB_SCHEMA, EXPECTED_SCHEMA_REVISION, quoted_schema
from souk.errors import AgentInUse, AgentNotFound, InvalidRegistration, LlmProviderNotFound
from souk.handlers import make_handlers
from souk.health import run_health_sweeps_forever
from souk.identity import (
    SoukIdentity,
    agent_deletion_signing_payload,
    is_timestamp_fresh,
    llm_registration_signing_payload,
    registration_signing_payload,
    verify_signature,
)
from souk.ids import new_id
from souk.kyok import ConnectedLLMProvider, KyokRelay
from souk.models import AgentRecord, AgentRef, AgentSummary, LlmRef, RunRecord

logger = logging.getLogger("souk.core")


@dataclass
class Registration:
    """What a provider gets back for proving who it is.

    `agents` are the pairs it just registered, indexed by name — which it
    already knew, since
    it chose the names and holds the key. souk used to hand back ids it had
    minted, and a provider that held those could be cut off from its own work
    by a database it never saw replaced.
    """

    agents: dict[str, AgentRef]


@dataclass(frozen=True)
class Health:
    """Whether this souk can do its job, as facts rather than a verdict.

    Deliberately not a bool: "the process is alive" and "it can serve
    traffic" are different questions with different answers, and only the
    caller knows which one it is asking. A gateway maps these onto liveness
    and readiness probes; an embedder may just log them.

    Carries no connection string, no driver message and no exception text.
    A readiness endpoint is normally unauthenticated, and a driver's error
    for an unreachable database routinely contains the host and user it
    tried — `database_error` is the exception's type name and nothing else.
    """

    database: bool
    schema_revision: str | None
    expected_schema_revision: str
    background_running: bool
    # Whether the broker's loop is turning. Separate from
    # `background_running` because they answer different questions: the
    # sweeps tidy up after runs, this one *is* how a run reaches a provider.
    dispatching: bool = False
    database_error: str | None = None

    @property
    def schema_current(self) -> bool:
        return self.schema_revision == self.expected_schema_revision

    @property
    def ready(self) -> bool:
        """Can this souk serve? The database has to be reachable and at the
        migration this code was built against — a process pointed at an
        unmigrated database would otherwise discover it as a missing column
        halfway through someone's request.

        Dispatch counts, and did not always: a souk whose broker loop is not
        turning accepts every run and hands none of them over, so reporting
        it ready sends traffic to something that will swallow it silently.
        That was true here — `ready` said yes while `Souk.start` had never
        been called and nothing could be dispatched at all.

        The health *sweeps* still deliberately do not count. Not running them
        is a degraded state, not an unservable one: runs are dispatched,
        produced and recorded without them, and what is lost is tidying up
        after the ones that go wrong.
        """
        return self.database and self.schema_current and self.dispatching


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


def _complete_run_agent_input(thread_id: str, run_id: str, run_input: dict[str, Any]) -> dict[str, Any]:
    """`start_run`/`resume_run`'s caller-supplied `run_input` filled out into
    a real `RunAgentInput` before dispatch.

    The library facade let a caller pass a bare `{"messages": [...]}` straight
    through to a provider — `state`/`tools`/`context`/`forwardedProps` never
    got filled in, unlike the AG-UI and A2A protocol adapters, which both
    build a real `RunAgentInput` via this same function before enqueueing.
    A `SoukLink.deliver` that validates against that type (as
    `souk-provider-sdk`'s now does) turned the gap from "a provider reads a
    missing key as None" into every delivery through this facade failing
    validation and the run never starting — caught by a real probe, not by
    reading the code.

    Message `id` is the other thing this facade never assigned: the protocol
    adapters get theirs from `repo.append_thread_messages`, which this facade
    deliberately does not call (see this method's own docstring — persisting
    the caller's messages is a protocol surface's job). `ag_ui.core.Message`
    requires one regardless, so one is minted here for whichever message
    arrived without it; it is real, not a placeholder, because nothing
    downstream of this call will assign a better one.
    """
    messages = [
        m if m.get("id") else {**m, "id": new_id("msg")} for m in run_input.get("messages", [])
    ]
    return build_run_agent_input(
        thread_id,
        run_id,
        messages,
        state=run_input.get("state"),
        tools=run_input.get("tools"),
        context=run_input.get("context"),
        forwarded_props=run_input.get("forwardedProps"),
        resume=run_input.get("resume"),
    )


class Souk:
    """One configured souk. Construct with explicit settings, or with none
    to resolve them from the `SOUK_*` environment variables:

        souk = Souk()                                    # all from env
        souk = Souk(CoreSettings(database_url="..."))    # explicit
    """

    def __init__(self, settings: CoreSettings | None = None, broker: RunBroker | None = None) -> None:
        self.settings = settings or CoreSettings()
        # Built here rather than on first use, so a malformed key fails the
        # process at construction instead of failing the first provider that
        # asks this souk to prove itself. None when unconfigured, which is a
        # supported state — see `CoreSettings.identity_private_key`.
        self.identity = (
            SoukIdentity.from_hex(self.settings.identity_private_key)
            if self.settings.identity_private_key
            else None
        )
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
        # Its entries live exactly as long as the broker holds the run,
        # which is why it listens on the broker's forget funnel rather
        # than trusting anyone to remember a cleanup call.
        self.kyok_relay = KyokRelay()
        self.broker.add_forget_listener(self.kyok_relay.discard)
        # Every background task this souk started — see spawn().
        self._tasks: set[asyncio.Task] = set()
        # Whether start() has run. Not "is the sweeper alive": a second
        # start() must not reconcile again (see start), so this records the
        # act, not the state.
        self._started = False
        # Anyone watching this souk from inside the process — see on_change.
        # A set, so subscribing twice with the same callable is once.
        self._change_subscribers: set[Callable[[ChangeEvent], None]] = set()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A database session scoped to a block — the direct replacement for
        `async with SessionLocal() as session:`."""
        async with self.sessionmaker() as session:
            yield session

    # ---- Who this souk is

    @property
    def identity_public_key(self) -> str | None:
        """This souk's Ed25519 public key, or None if it has no identity.

        What a provider pins so it can tell this souk from another one. None
        is a real answer and not an error: an unconfigured souk cannot prove
        itself, which is what every souk did before this existed.
        """
        return self.identity.public_key if self.identity is not None else None

    def sign(self, payload: bytes) -> str:
        """Prove this souk holds its key, over bytes somebody else chose.

        The mirror of what a provider does at registration, and the half that
        was missing: `verify_signature` has always taken arbitrary bytes, so
        souk could check anyone while being uncheckable itself.

        What to sign is deliberately not decided here. Proving identity as a
        connection opens is a serving act — the payload belongs to whoever
        serves souk, and core supplies the primitive it is built from.

        Raises `RuntimeError` if this souk has no identity, rather than
        returning something unusable: a caller reaching this without a key
        configured has a deployment problem, and a signature nobody can
        verify would hide it until a provider rejected the handshake.
        """
        if self.identity is None:
            raise RuntimeError(
                "this souk has no identity: set identity_private_key "
                "(SOUK_IDENTITY_PRIVATE_KEY) to a hex-encoded Ed25519 seed"
            )
        return self.identity.sign(payload)

    # ---- Lifecycle

    async def start(self) -> list[str]:
        """Bring this souk up: reconcile what the last process left behind,
        then keep the health sweeps running. Returns the run_ids it gave up
        on, which it also logs.

        The counterpart to `aclose`, and the reason it exists at all: live
        dispatch state is in memory, so a run still `queued` or `running` in
        the database when a process starts will never be picked up or
        completed by anyone — nothing consults the database for work. Saying
        so is the only honest thing to do with it.

        **Runs once.** A second call is a no-op, which matters more than it
        sounds: reconciliation is idempotent over rows from *before* the
        process started, not over a run created since, and a second pass
        would mark that one failed. The serving layer used to call this
        twice on purpose — once before opening its listeners and again from
        the ASGI lifespan — with a comment explaining why that was harmless.
        It was harmless only because the window between the two was usually
        empty.

        **Required.** It used to be optional — a caller that skipped it lost
        the reconciliation above and the health sweeps, but runs still
        dispatched, because a provider came and took them. souk hands work
        over now, and the thing that hands it over is the loop this starts,
        so a souk that was never started accepts every run and dispatches
        none of them. That paragraph stayed here after it stopped being true
        and was measured before it was rewritten: `start_run` returned a
        handle, the caller waited on its events, and three seconds later
        there was no event, no error and no log.

        `RunBroker.enqueue_run` refuses rather than queueing into a loop that
        will never come round, so this is now a mistake with a message
        attached to it.
        """
        if self._started:
            return []
        self._started = True
        async with self.session() as session:
            orphaned = await repo.fail_orphaned_runs(session)
        if orphaned:
            logger.warning(
                "start: marked %d run(s) failed — still queued/running from before this "
                "process, and souk's dispatch state does not survive a restart: %s",
                len(orphaned),
                orphaned,
            )
        # The broker's own loop. souk starting *is* the broker starting:
        # there is no state in which souk is up and dispatch is not, and
        # nothing else would know when to begin.
        self.broker.start()
        self.spawn(run_health_sweeps_forever(self), name="health-sweeps")
        return orphaned

    async def health(self, timeout: float = 2.0) -> Health:
        """Ask the database whether it is there and what schema it is at.

        Bounded, because a health check that hangs is worse than one that
        fails — a probe blocked on an unreachable database reports nothing at
        all, while the process it was meant to describe keeps taking traffic.
        A timeout is reported as unreachable.
        """
        revision: str | None = None
        reachable = True
        error: str | None = None
        try:
            async with asyncio.timeout(timeout):
                async with self.session() as session:
                    # Reachability first and on its own: anything after this
                    # may legitimately answer None, and "no answer" must not
                    # be able to stand in for "no database".
                    await session.execute(text("SELECT 1"))
                    revision = await repo.get_schema_revision(session)
        except TimeoutError:
            reachable, error = False, "TimeoutError"
        except Exception as exc:
            # The type only: see Health's docstring on what a driver puts in
            # the message.
            reachable, error = False, type(exc).__name__

        return Health(
            database=reachable,
            schema_revision=revision,
            expected_schema_revision=EXPECTED_SCHEMA_REVISION,
            background_running=any(
                t.get_name() == "health-sweeps" and not t.done() for t in self._tasks
            ),
            dispatching=self.broker.is_running,
            database_error=error,
        )

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

        Safe whether or not `start` was ever called — there is simply less
        to stop.

        Cancels in-flight background work and waits for it to unwind, so
        handlers get to finish their current statement rather than being
        killed mid-write. Runs still live at that point stay 'running' in the
        database and are reconciled on the next start (repo.fail_orphaned_runs)
        — souk's dispatch state is in-memory by design and does not survive a
        restart.
        """
        self.broker.stop()
        # So a later start() is a real start rather than a silent no-op —
        # the engine survives dispose (it refills its pool on demand), so
        # start/aclose/start is a lifecycle someone will reasonably expect
        # to work rather than to quietly do nothing.
        self._started = False
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
            registered = await repo.register_agents(
                session,
                public_key,
                agents,
                provider_name=provider_name,
            )
        # A name this key used to offer and did not mention is one it has
        # stopped offering, so it stops being served — announcing a smaller
        # roster while the broker still hands that agent work would be souk
        # contradicting itself.
        withdrawn = [
            a for a in self.broker.agents_served_by(public_key) if a.name not in registered
        ]
        if withdrawn:
            self.broker.unregister_provider(withdrawn)
        # Covers more than "an agent appeared": re-registering without a
        # name withdraws it, so this is also how a removal is announced.
        self._notify_change(RosterChanged())
        return Registration(agents=registered)

    async def delete_agent(
        self, public_key: str, name: str, signature: str, timestamp: int
    ) -> None:
        """Remove an agent this key registered and nothing has ever used.

        Signed, like registering, and for the same reason: an agent belongs to
        a keypair, and sharing souk's process is not evidence of holding it.
        The payload is domain-separated from a registration's — before that,
        the two were byte-identical for a single agent, so observing a
        provider register one was enough to hold a valid order to delete it
        (measured, not imagined; see souk/identity.py).

        Refused unless all four hold. The first three are "nothing is using it
        right now"; the fourth is "nothing ever did":

        - not online — a provider still checking in is still serving it;
        - no attached in-process worker — a wedged worker can be offline *and*
          attached, and both are evidence;
        - no active run, `input-required` included: that one is paused on a
          human who is coming back, and it is what a narrower liveness check
          would miss;
        - **no threads at all.** A thread must name an agent, so an agent with
          threads cannot be removed — the foreign key and the rule are the
          same statement rather than an obstacle to work around. It also means
          this can never reach a caller's messages, which souk stores
          deliberately so it is a source of truth for the whole conversation
          rather than half of it.

        So what is left is a single-row delete with no cascade. `AgentInUse`
        carries which check refused.
        """
        if not is_timestamp_fresh(timestamp):
            raise InvalidRegistration("deletion timestamp too far from souk's clock")
        if not verify_signature(
            public_key, signature, agent_deletion_signing_payload(name, timestamp)
        ):
            raise InvalidRegistration("invalid deletion signature")

        agent = AgentRef(provider_key=public_key, name=name)
        async with self.session() as session:
            record = await repo.get_agent(session, agent)
            if record is None:
                raise AgentNotFound(f"agent '{agent}' is not registered")
            # One guard where there were two. They asked the same question
            # by different means — "seen recently" and "attached here" — back
            # when souk could only infer the first from timestamps. It can see
            # it now, so the inference is gone and so is the second reason.
            if self.broker.serving(agent) is not None:
                raise AgentInUse(
                    f"agent '{agent}' has a provider serving it", reason="connected"
                )

            active = await repo.count_runs_for_agent(
                session, agent, repo.ACTIVE_RUN_STATUSES
            )
            if active:
                raise AgentInUse(
                    f"agent '{agent}' has {active} active run(s)", reason="active_run"
                )
            # Checked alongside threads rather than trusting that one implies
            # the other — see repo.count_threads_for_agent.
            if await repo.count_threads_for_agent(session, agent) or await repo.count_runs_for_agent(
                session, agent
            ):
                raise AgentInUse(
                    f"agent '{agent}' has a conversation behind it and cannot be removed — "
                    "stop offering it instead, and it goes offline and off the roster with "
                    "its record intact",
                    reason="has_history",
                )

            await repo.delete_agent(session, agent)
        self._notify_change(RosterChanged())

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
        if run.claimed_by is None:
            # Nobody holds this run as far as souk knows, and yet somebody is
            # producing for it. If that somebody is the provider registered
            # for its agent, this is an ack that arrived after souk stopped
            # waiting — take it, rather than throwing away real output and
            # then giving up on a run that is running (see
            # RunBroker.accept_late_ack).
            if not self.broker.accept_late_ack(run_id, claimed_by):
                logger.warning(
                    "report_event: '%s' reported for run %s, which nobody holds",
                    claimed_by,
                    run_id,
                )
                return False
        elif run.claimed_by != claimed_by:
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
        self, provider: ConnectedProvider, agent_names: list[str]
    ) -> None:
        """Serve these agents from this process.

        `provider` is anything souk can hand a run to — a public key, a way
        to take a run, a way to be asked to stop one. That is
        `broker.ConnectedProvider` and it is the whole of what souk knows
        about anybody: `souk_provider_sdk.ProviderRuntime` is one, wrapping an
        ordinary AG-UI agent, and so is whatever a gateway builds around a
        socket. No `provider_id` argument, because the provider already says
        who it is and being told a second time only creates a way to disagree.

        Three things, and deliberately not a fourth:

        - every name must be one this key registered, and `AgentNotFound` for
          one it did not — sharing souk's process is not a reason to skip
          registration and not a reason to take a different path;
        - mark them seen, so an attached provider is online from the moment it
          attaches rather than from whenever it is first given work;
        - tell the broker where those agents' runs go.

        What it no longer does is run anything. It used to build a worker —
        souk's own claim loop, driving the provider object — so souk chose the
        concurrency, the pacing and the error handling of something that is
        not souk. Those are the provider's, and it has its own loop to put
        them in.

        Attaching the same key again replaces the mapping for those names,
        which is what a reconnect is; runs it already holds are untouched, as
        is its capacity count.
        """
        if not agent_names:
            raise ValueError(
                f"provider '{provider.public_key}' attached with no agent names — "
                "there would be nothing to serve"
            )
        async with self.session() as session:
            registered = await repo.get_agent_names_for_provider(
                session, provider.public_key
            )
        unknown = sorted(set(agent_names) - registered)
        if unknown:
            raise AgentNotFound(
                f"provider '{provider.public_key}' has not registered {unknown} — "
                "register before attaching, in-process or not"
            )

        self.broker.register_provider(
            {
                AgentRef(provider_key=provider.public_key, name=name): provider
                for name in agent_names
            }
        )
        async with self.session() as session:
            await repo.touch_agents(session, provider.public_key, agent_names)
            await session.commit()
        # Reachability is part of what the roster answers, so this changes it
        # even though no agent was added.
        self._notify_change(RosterChanged())

    async def register_llm_providers(
        self,
        public_key: str,
        signature: str,
        timestamp: int,
        names: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, LlmRef]:
        """Prove an identity holds its key, then record which model
        offerings it answers KYOK completions under.

        The same act as register_agents with the same machinery — an
        Ed25519 signature over an operation-prefixed payload covering the
        claimed names, freshness bounded — because an LLM provider's
        identity is the same kind of thing as an agent provider's, and an
        offering is `(provider_key, name)` exactly as an agent is.
        """
        if not is_timestamp_fresh(timestamp):
            raise InvalidRegistration("registration timestamp too far from souk's clock")
        payload = llm_registration_signing_payload(names, timestamp)
        if not verify_signature(public_key, signature, payload):
            raise InvalidRegistration("invalid LLM provider registration signature")
        async with self.session() as session:
            registered = await repo.register_llm_providers(
                session, public_key, names, metadata
            )
        self._notify_change(LlmRosterChanged())
        return registered

    async def attach_llm_provider(
        self, link: ConnectedLLMProvider, model_names: list[str]
    ) -> None:
        """Serve these models' KYOK completions from this connection —
        the mirror of attach_provider, rule for rule: the link says who it
        is, the names say what it is serving right now, every name must be
        one this key registered, and sharing souk's process is not a
        reason to skip any of it.

        Attaching the same identity again replaces the mapping for those
        names, which is what a reconnect is. Completions resolve the
        connection per call (see KyokRelay), so runs bound to these
        offerings simply start reaching the new link.
        """
        if not model_names:
            raise ValueError(
                f"LLM provider '{link.public_key}' attached with no model names — "
                "there would be nothing to serve"
            )
        async with self.session() as session:
            registered = await repo.get_llm_names_for_key(session, link.public_key)
        unknown = sorted(set(model_names) - registered)
        if unknown:
            raise LlmProviderNotFound(
                f"LLM provider '{link.public_key}' has not registered {unknown} — "
                "register before attaching, in-process or not"
            )
        self.kyok_relay.attach(
            {
                LlmRef(provider_key=link.public_key, name=name): link
                for name in model_names
            }
        )
        async with self.session() as session:
            await repo.touch_llm_providers(session, link.public_key, model_names)
            await session.commit()
        # Reachability is part of what the roster answers, so this changes
        # it even though no offering was added — same sentence as
        # attach_provider, deliberately.
        self._notify_change(LlmRosterChanged())

    def detach_llm_provider(self, public_key: str) -> None:
        """This LLM provider is gone from this process. Runs bound to its
        offerings are left alone — a binding names an offering, not a
        connection, so completions for them start failing (503) until it
        attaches again, and souk records nothing it has not observed.

        No database write, for detach_provider's reason verbatim:
        unregistering *is* going offline, and reachability is read from
        the relay, not from `last_seen_at`."""
        if not self.kyok_relay.serving_any(public_key):
            return
        self.kyok_relay.detach(public_key)
        self._notify_change(LlmRosterChanged())

    async def detach_provider(self, provider_public_key: str) -> None:
        """This provider is gone from this process.

        A remote provider's absence can only be inferred once it stops
        answering; this one is a departure souk witnessed, so its agents go
        offline at once instead of ageing out of the window.

        Runs it already holds are left alone. It may still be producing, and
        souk records no outcome it has not observed — if it has really gone,
        the health sweep is what notices, from the run's own silence.
        """
        attached = self.broker.agents_served_by(provider_public_key)
        if not attached:
            return
        self.broker.unregister_provider(attached)
        # No database write. Unregistering *is* going offline: `last_seen_at`
        # used to be backdated here to make the roster agree, and there is
        # nothing left to make agree — the roster reads reachability from the
        # broker. Backdating it now would only bring forward the day this
        # agent is hidden from the roster altogether, which is not what
        # detaching means.
        self._notify_change(RosterChanged())

    # ---- Watching a souk from inside the process

    def on_change(self, callback: Callable[[ChangeEvent], None]) -> Callable[[], None]:
        """Be told when souk's own state changes, instead of asking again.

        Returns the unsubscribe. `callback` is called synchronously, on
        whichever task made the change, and is not awaited — the same
        contract as `ConnectedProvider.cancel`, and for the same reason: souk
        is telling you, not handing you a job. Keep it short and do the real
        work elsewhere; a slow callback slows down the run that triggered it.

        No history, no replay, no ordering guarantee across subscribers. What
        a subscriber does with an event is re-query (see `list_agents`,
        `get_run`) — the database stays the thing that is true, and this only
        saves you from polling it.

        Raising is contained: one broken subscriber must not fail the
        registration or the run that notified it. It is logged, not silenced.
        """
        self._change_subscribers.add(callback)

        def unsubscribe() -> None:
            self._change_subscribers.discard(callback)

        return unsubscribe

    def _notify_change(self, event: ChangeEvent) -> None:
        # Iterate a copy: a callback is allowed to unsubscribe itself.
        for callback in list(self._change_subscribers):
            try:
                callback(event)
            except Exception:
                logger.exception("on_change subscriber raised for %r", event)

    async def mark_run_status(
        self, session: AsyncSession, run_id: str, status: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """Record a run's status *and* announce it. The one way souk changes
        a run's status.

        It exists because the alternative is remembering to notify at each of
        the seven places that move a run — and the eighth, added later, is
        the one that silently does not. `repo.mark_run_status` is the storage
        half and is not called directly from anywhere else;
        tests/test_change_hook.py asserts that rather than trusting it.
        """
        await repo.mark_run_status(session, run_id, status, metadata=metadata)
        self._notify_change(RunStatusChanged(run_id=run_id, status=status))

    async def list_agents(self) -> list[AgentSummary]:
        """The roster: what is registered, and which of it can be reached.

        Two sources, because they are two different facts. What exists is
        stored; whether anybody is serving it is live, and only this process
        can answer it today (see `RunBroker.serving`).
        """
        async with self.session() as session:
            stored = await repo.list_agents(
                session,
                stale_hidden_window_seconds=self.settings.stale_hidden_window_seconds,
            )
        return [
            summary.model_copy(update={"online": self.is_serving(
                AgentRef(provider_key=summary.provider_key, name=summary.name)
            )})
            for summary in stored
        ]

    def is_serving(self, agent: AgentRef) -> bool:
        """Is anybody serving this agent through this souk right now.

        The one question every caller asks about reachability, so protocols
        ask souk rather than reaching into the broker themselves.
        """
        return self.broker.serving(agent) is not None

    async def get_agent(self, agent: AgentRef) -> AgentRecord | None:
        async with self.session() as session:
            return await repo.get_agent(session, agent)

    async def resolve_agent(self, provider: str, name: str) -> AgentRecord | None:
        """Which agent a provider means by a name — `provider` being its
        public key, the only identity it has.

        The unambiguous way to address an agent without knowing souk's own
        id for it: the pair is the natural key (`UNIQUE(public_key, name)`),
        so this either finds one agent or none, and a caller has nothing to
        disambiguate.

        There is deliberately no by-display-name sibling. Browsing for who
        offers a name is `list_agents`, which answers with all of them; the
        lookup that took a bare name and hoped for exactly one is gone —
        see the note in `docs/library-architecture.md`.
        """
        async with self.session() as session:
            return await repo.resolve_agent(session, provider, name)

    # ---- Threads

    async def create_thread(
        self, agent: AgentRef, parent_thread_id: str | None = None, metadata: dict | None = None
    ) -> str:
        async with self.session() as session:
            thread_id = await repo.create_thread(session, agent, parent_thread_id, metadata)
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
                "provider_key": root["provider_key"],
                "agent_name": root["agent_name"],
                "children": await build(thread_id),
            }

    # ---- Runs

    async def get_run(self, run_id: str) -> RunRecord | None:
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
        agent: AgentRef,
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
            run_id, agent, thread_id, input_json, protocol, make_handlers(self), seq=seq
        )

    async def start_run(
        self,
        agent: AgentRef,
        run_input: dict[str, Any],
        thread_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RunHandle:
        """Start a run against `agent` and hand back a handle to it.

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
                session, agent, thread_id, metadata=metadata, create_if_missing=True
            )
            created = await repo.create_run(
                session, resolved_thread_id, agent, "ag-ui", run_input, metadata
            )
            run_id = created["run_id"]

        self.enqueue_run(
            run_id,
            agent,
            resolved_thread_id,
            _complete_run_agent_input(resolved_thread_id, run_id, run_input),
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
            AgentRef(provider_key=stored.provider_key, name=stored.agent_name),
            stored.thread_id,
            _complete_run_agent_input(stored.thread_id, run_id, run_input),
            stored.protocol or "ag-ui",
            seq=starting_seq,
        )
        return RunHandle(
            run_id=run_id,
            thread_id=stored.thread_id,
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
