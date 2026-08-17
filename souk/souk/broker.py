"""In-memory hand-off between a run's caller and the worker holding it.

Neither side is named by transport here, deliberately. A caller reaches a
run through a protocol adapter, and a worker reaches it through core's three
methods (`deliver` / `report_event` / `finish_run`); what carried either
of them is a serving-layer choice this module must not encode. It once
said "the HTTP gateway and the gRPC relay", which was true of exactly one
deployment.

Each run is modeled like a tiny OS process: a `Run` is pure data (no
methods — see its docstring), with two queues attached to it playing the
role of stdin/stdout:

- `in_queue` ("stdin"): every external actor that wants to *affect* a run
  — the worker holding it reporting an event or the end of its stream
  (see souk.core's report_event/finish_run, whichever transport carried
  it), an explicit A2A tasks/cancel, the health sweep giving up on a
  stalled/abandoned run — does so by pushing one `Command` here. This is
  the *only* routing table an event crosses: a worker's frame is looked
  up here by run_id and pushed, with no second per-run table in between
  (see docs/library-architecture.md on the worker model).
  Nobody else ever touches a Run's fields directly. An HTTP/SSE consumer
  disconnecting is *not* one of these: souk's own DB state is
  authoritative regardless of whether anyone's still watching a
  particular stream, so a dropped connection alone never cancels a run —
  see protocols.agui's AGUIAdapter.run's event_stream for the reasoning.
- `out_queue` ("stdout"): AG-UI events the run emits, drained by whichever
  HTTP/SSE consumer (api_agui.py / api_a2a.py) is watching it.

Exactly one task per run (`_pipeline`, spawned by `enqueue_run`) reads
`in_queue` and is the *only* code that ever mutates a Run's fields —
dispatching each command to a handler function ("function objects", see
souk/handlers.py) supplied by the caller — with one deliberate
exception: `Run.cancel_requested` (see its own docstring below). Everything
*else* about cancelling a run — the DB write, telling the agent to stop —
is genuinely multi-step, has to happen in order relative to this run's
other commands, and belongs on the pipeline like everything else. Whether
a run *is* cancelled is a single, atomically-set bool, and the whole
system needs to see it change the instant it's requested, not whenever
the pipeline gets around to a queued command — see `request_cancel`.

Both operations here are synchronous, not an `await` — request_cancel's
caller (only ever an explicit A2A tasks/cancel; see above) never has to
wait on the pipeline task to get scheduled, and the actual multi-step
work (DB write, signaling the agent) happens later, on the run's own
independent task, never racing a second caller over the same fields.

(This replaced an earlier version where four unrelated modules each
poked a shared dataclass's fields directly, and cancellation needed two
rounds of hand-tuned `asyncio.create_task`/ordering fixes to work at all;
then a version where *every* field, cancelled included, only changed on
the pipeline task, which closed that race but reopened a different one —
broker.poll() hands out already-cancelled runs because it can't see
`cancelled` go true until the pipeline processes a queued command that
just hasn't run yet. See git history on this file for both.)

A RunBroker instance belongs to one Souk (see souk/core.py) rather than
being a module-level singleton — same reasoning as its settings and engine,
and what would let a distributed implementation substitute here without
touching any caller (see docs/library-architecture.md on horizontal
scaling).

One souk is one process on one event loop — callers and workers alike
reach it in-process, whatever carried them there — so all of this is
implemented with plain asyncio primitives rather than round-tripping
through the database. The database (souk/schema.py) is the durable record for anything that needs to survive a
restart or be queried after the fact (roster, thread history, run
status, run_events) — it is not on the live event-relay hot path.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from souk.models import AgentRef, ClaimedRun

logger = logging.getLogger("souk.broker")

# Sentinel put on a run's out_queue to signal the stream has ended.
END_OF_STREAM = object()


# ---- Commands: pure data, pushed onto a run's in_queue. What each one
# means and who's allowed to handle it lives in whoever supplies the
# HandlerMap (see souk.handlers) — broker.py only needs to know these
# exist, not what they do.


@dataclass
class Claim:
    """A worker has taken this run — pushed by `RunBroker.claim`, which
    hands the run's input straight to the claimer.

    No payload: who took it is recorded on the Run itself, synchronously,
    in the same step that hands it over (see `claim`). This used to carry
    an AgentProvider object for core to call back into; it doesn't,
    because core no longer calls anyone — a worker pushes (see
    souk/worker.py). What is left is a marker that the hand-over happened,
    ordered on the run's own pipeline against everything else about it.
    """


@dataclass
class RelayEvent:
    """One AG-UI event a worker produced for this run — as its own
    payload, not a wire frame; whatever transport carried it has already
    been peeled off by the time this exists (see souk.core's
    `report_event`, the one door events come in by).
    """

    event: Any


@dataclass
class FinishStream:
    """The agent sent end_of_stream — the one true, authoritative signal
    that it's done producing events for this run, whether it finished
    normally, paused, or is unwinding after a RequestCancel.
    """


@dataclass
class RequestCancel:
    """The async, multi-step half of a cancellation — DB write, telling
    the agent to stop, sending out_queue its END_OF_STREAM — handled by
    handlers._handle_cancel on the run's own pipeline task. Pushed by
    `request_cancel` below, never directly — see that function for the
    synchronous half (`Run.cancel_requested`) this doesn't cover.
    """


@dataclass
class Fail:
    """The health sweep gave up on this run (stalled or never claimed) —
    see souk/health.py. Carries the reason for the terminal RUN_ERROR
    event/failure metadata.
    """

    reason: str


Command = Claim | RelayEvent | FinishStream | RequestCancel | Fail


@dataclass(frozen=True)
class ProviderQuality:
    """What souk has observed about a provider keeping its word.

    Three ways of not doing what it said, all of them things souk saw rather
    than inferred:

    - `misdeclared` — it declared a capacity and then refused work inside it,
      so souk learned the real number by being refused;
    - `abandoned` — it took a run and then neither finished it nor reported
      it failed, so the stall sweep had to give up;
    - `unanswered` — it was offered a run and said nothing at all;
    - `answered_late` — of those, the ones it had taken anyway, found because
      it started producing for a run souk had already re-queued.

    In memory, and so about *this process since it started*. That is the
    honest scope: a provider that behaved badly yesterday, in a souk that has
    since restarted, is not something this souk witnessed.
    """

    in_flight: int
    declared: int | None
    misdeclared: int
    abandoned: int
    unanswered: int
    answered_late: int


@dataclass
class _Capacity:
    """What souk believes a provider can take, and what it has learned.

    A bucket souk keeps rather than a number the provider keeps telling it:
    souk sees every run start and every run end, so it can count for itself,
    and a count derived from events it already handles cannot go stale
    between them.

    Per provider, not per agent — one identity serving a translator and a
    summarizer has one process behind both, and its budget is across them.
    """

    # How many at once, declared when the provider registered. None is
    # unlimited: souk offers everything and lets the provider decline if that
    # was optimistic.
    declared: int | None
    in_flight: int = 0
    # Times it declined while souk believed it had room. Not bookkeeping —
    # souk observing that what this provider says it can take and what it
    # actually takes disagree.
    misdeclared: int = 0
    # Runs it took and never ended: reaped by the stall sweep rather than
    # finished or failed by the provider. Each held a place in this bucket
    # for the whole stall timeout.
    abandoned: int = 0
    # Runs offered that it never answered — no ack, no refusal, nothing
    # before the deadline.
    unanswered: int = 0
    # Of those, the ones it turned out to have taken anyway: it started
    # producing for a run souk had already put back in the queue.
    #
    # The worst signal here. Delivery runs over TCP, so an ack is not lost in
    # transit — the connection breaks instead. A late one means the provider
    # was simply too slow to say yes, which is its own doing, and souk had
    # meanwhile re-queued a run it was already running. One more offer and it
    # would have run twice.
    answered_late: int = 0

    @property
    def has_room(self) -> bool:
        return self.declared is None or self.in_flight < self.declared


class ConnectedProvider(Protocol):
    """Whoever is serving an agent right now, as the broker sees them.

    Four things, because they are the four the broker needs and no more: who
    this is, how much it will take at once, how to hand it a run, and how to
    ask it to stop one. What carries any of them — a call in this process, a
    frame on a socket — is not the broker's business and does not appear here.

    It said "three" until this docstring was corrected, having gained
    `max_concurrent_runs` without it. That is the easiest of the four to miss,
    because it is the only one that is not a call: a provider without it
    constructs and attaches perfectly well, then fails inside
    `register_provider` when the broker sizes its capacity bucket. Anything
    counting the members of this protocol should count the annotations too,
    not just the methods.

    A `Protocol` rather than a base class, deliberately. The implementations
    live in other distributions and one of them must be able to exist without
    ever importing souk:

    - `souk_provider_sdk.InProcessLink` — a direct call, no wire
    - AgentSoukServer's `SocketProvider` — a frame, and an ack to wait on

    They are not the same kind of object, which is worth knowing before
    reading either. The first is provider-side and subclasses
    `souk_provider_sdk.SoukLink`, where this `ClaimedRun` is translated into
    that package's own delivered-run type — once, rather than per transport.
    The second is gateway-side, holds an outbound queue and no runtime, and
    satisfies this protocol directly; it neither does nor should import that
    package. Souk requires nothing of either: anything with these four
    members works, whether or not it has ever heard of the SDK.
    """

    # The provider's Ed25519 public key. Established when it connected, not
    # per run: recorded on each run it takes, and every event it later
    # reports is checked against it (see Souk.report_event), because holding
    # a connection is not the same as holding a particular run.
    public_key: str
    # How many runs it will have going at once, across every agent it serves.
    # None is unlimited. souk keeps a bucket this size and offers nothing once
    # it is empty — see _Capacity.
    max_concurrent_runs: int | None

    async def deliver(self, run: ClaimedRun) -> bool:
        """Take this run, or decline it.

        What it is given is a `ClaimedRun` — the run's identity, its
        agent and its input — and never `broker.Run`, which is souk's own
        dispatch state with the run's queues hanging off it. A provider
        holding that could reach into souk's machinery in-process, and
        could not be handed anything at all over a wire. The offer is a
        value, so the same call means the same thing either way.

        True is the ack, and it means the run has started: from that moment
        the broker records who has it and the run moves to `running`.
        Anything else — False, or an exception — leaves the run exactly where
        it was, queued, to be offered again.

        Declining is how a provider says it is full. It is the only way it
        can: souk cannot see a provider's capacity, so the provider expresses
        it by not taking work.
        """
        ...

    def cancel(self, run_id: str) -> None:
        """souk is asking for this run to stop. A request, not an order —
        what the provider does about it is its own business, and souk decides
        the outcome from what the stream actually does."""
        ...


@dataclass
class Run:
    """Pure data — deliberately no methods. Everything that changes about
    a run happens by pushing a Command onto `in_queue` and letting this
    run's own pipeline task (see `_pipeline` below) apply it; nothing
    else should ever assign to these fields directly, including this
    module's own RunBroker (it only ever reads run_id/agent to route,
    never mutates) — except `cancelled`, see its own docstring just below.
    """

    run_id: str
    agent: AgentRef
    thread_id: str
    input_json: dict[str, Any]
    protocol: str  # "ag-ui" | "a2a"
    # When this run entered dispatch. Immutable, so keeping it in memory
    # costs nothing and saves the broker a database read to answer its own
    # question — how long has this been waiting for somebody to take it.
    #
    # Deliberately not `runs.created_at`: for a run resuming after a pause
    # that column still holds the original creation, while this is when the
    # current round started waiting. Waiting is what the broker times.
    queued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    seq: int = 0
    # The seq this round started at (== the `seq` ctor param — see
    # enqueue_run) — never mutated afterward, unlike `seq` itself, which
    # _handle_relay bumps for every event of this round. _handle_finish
    # uses the gap between the two to know which run_events rows belong
    # to *this* round only, so it doesn't re-reduce (and re-persist as
    # duplicate thread_history messages) events from an earlier
    # pause/resume round under the same run_id — see repo.reopen_run.
    round_starting_seq: int = 0
    pause_payload: dict[str, Any] | None = None
    # The public key of the provider that took this run, set by `claim`
    # in the same synchronous step that hands the input over. None until
    # someone takes it — which several places test for, since "never
    # claimed" and "claimed then cancelled" need different handling.
    #
    # Also what every reported event is checked against (see
    # souk.core.report_event): a provider may only speak for runs it was
    # actually given, and a connection alone is not evidence of that.
    claimed_by: str | None = None
    # How to *ask* the worker holding this run to stop — supplied by
    # whoever took it (see ConnectedProvider.cancel), because
    # only the claimer knows how to reach itself: an in-process worker
    # cancels its own task, a remote one puts a frame on its own wire.
    # Called by
    # handlers._handle_cancel, synchronously and at most once; it must
    # not block, and what the worker does about it is the worker's
    # business, not souk's.
    cancel_notify: Callable[[str], None] | None = None
    # "Someone asked for this run to stop" — NOT "this run was cancelled".
    # souk knows the first for certain; the second is only knowable once
    # the agent's stream actually ends, because a provider is free to
    # ignore the request and run to completion, and then the honest
    # outcome is `completed` (see handlers._handle_finish's decision).
    #
    # The one field anyone may set directly, synchronously, from any thread
    # of control — see request_cancel() below. Read by:
    #   - claim(), which won't hand out a run whose cancel was already
    #     requested. It marks the run claimed in the same synchronous
    #     step, so there is no window between the two for a request to
    #     land in — the "handed out but not yet claimed" race the old
    #     pull model had to cover in a second place is gone.
    #   - handlers._handle_finish, which needs it to tell "stopped early
    #     because we asked" apart from "stopped early because it broke".
    cancel_requested: bool = False
    # Set by _handle_relay when the agent emits a terminal event, so
    # _handle_finish can tell a run that genuinely finished from one whose
    # stream simply stopped. Absence is the only "it didn't finish" signal
    # AG-UI has — there is no cancelled event or outcome in the protocol.
    saw_run_finished: bool = False
    # Likewise for RUN_ERROR, which is a *different* question: RUN_FINISHED
    # decides the outcome, this one only records that the caller has already
    # been told something went wrong. _handle_finish emits a terminal
    # RUN_ERROR of its own when a run ends failed and nothing said so — this
    # is what keeps it from saying it twice for an agent that reported its
    # own failure properly.
    saw_run_error: bool = False
    in_queue: asyncio.Queue[Command] = field(default_factory=asyncio.Queue)
    out_queue: asyncio.Queue[Any] = field(default_factory=asyncio.Queue)


@dataclass(frozen=True)
class RunSnapshot:
    """What a caller outside the broker may know about a run.

    A copy, taken when you asked — ask again for a fresh one. Deliberately
    not the live `Run`: that object carries this run's two queues and the
    mutable state its pipeline is in the middle of changing, and handing it
    out is what made `Run` part of the contract between the broker and
    everything else rather than an implementation detail of the broker.
    Nothing here is a live reference, so a broker keeping its runs in another
    process can produce one just as well.
    """

    run_id: str
    agent: AgentRef
    thread_id: str
    protocol: str
    claimed_by: str | None
    cancel_requested: bool

    @property
    def is_claimed(self) -> bool:
        return self.claimed_by is not None


def _snapshot(run: Run) -> RunSnapshot:
    return RunSnapshot(
        run_id=run.run_id,
        agent=run.agent,
        thread_id=run.thread_id,
        protocol=run.protocol,
        claimed_by=run.claimed_by,
        cancel_requested=run.cancel_requested,
    )


def _request_cancel(run: Run) -> None:
    """The one correct way to cancel a run — call this, don't push
    RequestCancel directly. Splits into exactly the two halves described
    on `Run.cancel_requested` and `RequestCancel`: flips the flag immediately so
    the rest of the system (chiefly broker.poll(), and
    handlers._handle_claim's narrower fallback check for the case
    poll() already handed the run out) can react without delay, then
    queues the actual DB write / agent notification for the run's own
    pipeline task to carry out in order relative to its other commands.

    Cancelling a run is always an explicit act — see protocols.agui's AGUIAdapter.run's
    event_stream for why an HTTP/SSE consumer disconnecting must never
    call this. In practice that leaves A2A's tasks/cancel as the only
    caller, an ordinary request context, not one already being torn
    down — but both operations here are synchronous regardless (neither
    is an `await`), so this would still be safe to call from a context
    that was mid-teardown, if one ever needs to again.
    """
    run.cancel_requested = True
    run.in_queue.put_nowait(RequestCancel())


async def _drain_run(run: Run) -> AsyncIterator[Any]:
    """Yields whatever the run's pipeline pushes onto `out_queue`, in
    order, until `END_OF_STREAM`. The one piece of run-consumption logic
    shared by every protocol surface that watches a run (api_agui.py's
    event_stream, api_a2a.py's tasks/send and tasks/sendSubscribe) — each
    of those differs only in how it translates/collects what this yields,
    never in how it reads the queue or recognizes the end. Exiting this
    generator early (caller disconnects, breaks out of a `async for`)
    does not cancel the run — see Run.in_queue's docstring for why that's
    never implicit.
    """
    while True:
        item = await run.out_queue.get()
        if item is END_OF_STREAM:
            return
        yield item


async def _no_events() -> AsyncIterator[Any]:
    """A run this broker does not have produces nothing rather than raising:
    'already finished' and 'never here' are the same answer to a consumer,
    and both are ordinary."""
    return
    yield  # pragma: no cover - what makes this an async generator


HandlerMap = dict[type, Callable[[Run, Any], Awaitable[None]]]

async def _pipeline(run: Run, handlers: HandlerMap, owner: "RunBroker") -> None:
    """The run's own single-consumer task — the only thing that ever
    dispatches a Command to a handler, so no two handlers ever run
    concurrently against the same Run. Terminates (and forgets the run
    from the registry) on:
    - FinishStream — the agent's own authoritative "done" signal.
    - Fail — the health sweep gave up on this run outright (see
      handlers._handle_fail); no FinishStream is ever coming for one
      of these either.
    - RequestCancel, but only if the run was never claimed
      (`claimed_by is None`) — an explicit tasks/cancel (the only
      thing that ever pushes this; see broker.request_cancel) arriving
      before any worker claimed the run means no FinishStream will ever
      come for it. If it *was* already claimed, this does not terminate
      the pipeline by itself — it waits for the worker's own FinishStream
      once its agent unwinds after being asked to stop (see
      handlers._handle_cancel).
    """
    while True:
        cmd = await run.in_queue.get()
        handler = handlers.get(type(cmd))
        try:
            if handler is not None:
                await handler(run, cmd)
            else:
                logger.warning("run %s: no handler registered for %s", run.run_id, type(cmd).__name__)
        except Exception:
            logger.exception("run %s: error handling %s", run.run_id, type(cmd).__name__)
        if isinstance(cmd, (FinishStream, Fail)):
            break
        if isinstance(cmd, RequestCancel) and run.claimed_by is None:
            break
    # Closing the stream belongs here rather than to the handlers, and the
    # reason is the `except` just above: a handler that raises is logged and
    # the pipeline carries on, so a handler that ended a run by putting
    # END_OF_STREAM itself would skip it exactly when it failed — leaving
    # every consumer of that run waiting forever on a run nothing will ever
    # produce for again.
    #
    # Not hypothetical. `run_events.run_id` became a real foreign key when
    # runs got their own table, and the probe that wipes souk's tables mid-run
    # went from silently writing orphan rows to hanging: _handle_finish raised
    # on the failed insert, before its own put. The three terminating cases
    # are already exactly the three this loop breaks on, so there is one place
    # to put it and it is here.
    run.out_queue.put_nowait(END_OF_STREAM)
    owner.forget(run.run_id)


class RunBroker:
    def __init__(
        self,
        spawn=None,
        *,
        sweep_interval_seconds: float = 1.0,
        queued_timeout_seconds: float = 45.0,
        deliver_timeout_seconds: float = 5.0,
    ) -> None:
        # How a run's pipeline task gets started. A Souk passes its own
        # spawn (see souk/core.py) so the task is supervised and can be
        # cancelled and awaited at shutdown; the default keeps this class
        # usable on its own, which its tests rely on.
        self._spawn = spawn or self._spawn_unsupervised
        self._runs: dict[str, Run] = {}
        # Keyed by the agent itself, which is possible because an agent *is*
        # `(provider_key, name)` and AgentRef is frozen (see souk/models.py).
        # It used to be keyed by a souk-minted id standing in for that pair.
        self._pending_by_agent: dict[AgentRef, deque[str]] = defaultdict(deque)
        # Which provider is serving each agent. The whole of what the broker
        # knows about reaching anybody, and private on purpose: ask `serving`
        # or `agents_served_by`. Reachability is the one fact that stops being
        # answerable from one process the moment there is more than one souk,
        # so it gets a single door to swap rather than a dict several modules
        # reach into. See docs/broker-horizontal-scaling.md.
        self._providers: dict[AgentRef, ConnectedProvider] = {}
        # What each provider can take, by public key — one bucket per
        # identity, however many agents it serves.
        self._capacity: dict[str, _Capacity] = {}
        # Kept per run so the broker can start work for it later than
        # enqueueing: a run's pipeline begins when a provider takes it, and a
        # queued run that is cancelled needs one handler run and no pipeline.
        self._handlers: dict[str, HandlerMap] = {}
        self._pipeline_tasks: set[asyncio.Task] = set()
        # How often this broker's own loop comes round, and how long a run
        # may go unwanted before it gives up on it.
        #
        # The broker's own, not `CoreSettings`': which runs it is holding and
        # how long it has held them is a question only it can answer, and a
        # copy of the number in settings is a copy that can disagree with the
        # one actually used. It briefly did — settings carried
        # `queued_timeout_seconds` while nothing passed it here, so changing
        # it did nothing, and the two defaults being equal hid that.
        #
        # A deployment wanting a different number passes a configured broker:
        # `Souk(settings, broker=RunBroker(queued_timeout_seconds=...))`.
        self.sweep_interval_seconds = sweep_interval_seconds
        self.queued_timeout_seconds = queued_timeout_seconds
        # How long to wait for a provider to answer an offer. There is one
        # delivery loop, so an offer that never returns stops dispatch for
        # every agent, not only this provider's.
        self.deliver_timeout_seconds = deliver_timeout_seconds
        self._loop_task: asyncio.Task | None = None
        # Set whenever something might have made placing possible again — a
        # run arriving, or a provider registering. The sweep waits on this,
        # and only ever waits when there is nothing it could be doing.
        #
        # Rebuilt by `start`, not left as the one made here. An
        # `asyncio.Event` binds to the loop that first uses it, and a
        # `RunBroker` is constructed synchronously — possibly with no loop
        # running at all, and certainly not necessarily the loop it will run
        # in. Keeping this one made every later loop raise "bound to a
        # different event loop" the moment the sweep tried to rest.
        self._work_to_do = asyncio.Event()

    # ---- The broker's own loop

    def start(self) -> None:
        """Begin sweeping. Idempotent.

        Binds this broker to the loop that is running now — see
        `_work_to_do`. Nothing is lost by replacing it: `run_forever` makes a
        full placing pass before it ever waits, so a set that happened before
        this call is answered by that pass rather than by the event.
        """
        if not self.is_running:
            self._work_to_do = asyncio.Event()
            self._loop_task = self._spawn(self.run_forever(), name="broker-sweep")

    @property
    def is_running(self) -> bool:
        """Is the loop turning. The only thing that hands a run to anybody, so
        this is what "can this souk dispatch" means (see `Souk.health`)."""
        return self._loop_task is not None and not self._loop_task.done()

    def stop(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None

    async def run_forever(self) -> None:
        """Keep placing what is waiting, and rest only when it cannot.

        There are exactly two states this may sleep in:

        - **nothing is queued** — there is no work to place;
        - **nothing that is queued has a provider registered** — there is
          nowhere to place it.

        In any other state it keeps going. Sleeping on a fixed interval while
        runs sit placeable would add that interval to every one of them for
        no reason.

        When it does sleep it sleeps until woken rather than for a duration:
        `_work_to_do` is set by a run arriving and by a provider registering,
        which are the only two things that can change either condition. The
        wait is bounded by `queued_timeout_seconds` so a run nobody ever
        comes for is still given up on.

        A provider that declined everything is *not* one of those states: it
        is reachable and there is work for it, so the broker keeps asking.
        """
        while True:
            try:
                self.expire_queued(self.queued_timeout_seconds)
                self._work_to_do.clear()
                placed = False
                for agent in list(self._pending_by_agent):
                    if await self._offer_pending(agent):
                        placed = True
                if placed:
                    # Something moved, so more might. Yield and go again.
                    await asyncio.sleep(0)
                    continue
                # A whole pass placed nothing: everything queued is either
                # unserved or with a provider that will not take it. Wait for
                # something that could change that — a run arriving, a
                # provider registering, or one of a provider's runs ending
                # and giving its place back. All three set `_work_to_do`.
                #
                # Waiting rather than asking again is the point: asking again
                # is asking a provider that just said no, as fast as the loop
                # can turn, with nothing about the answer having changed.
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(self.queued_timeout_seconds):
                        await self._work_to_do.wait()
            except asyncio.CancelledError:
                raise
            except Exception:
                # One provider misbehaving must not stop the sweep for every
                # other agent.
                logger.exception("broker sweep failed; continuing")
                await asyncio.sleep(self.sweep_interval_seconds)

    def enqueue_run(
        self,
        run_id: str,
        agent: AgentRef,
        thread_id: str,
        input_json: dict[str, Any],
        protocol: str,
        handlers: HandlerMap | None = None,
        seq: int = 0,
    ) -> Run:
        """`handlers=None` skips spawning this run's pipeline task — only
        useful for tests exercising pure registry/poll/wake logic in
        isolation. Every real caller (api_agui.py, api_a2a.py,
        core's auto-resume path) must pass handlers.make_handlers,
        or nothing will ever consume commands pushed for this run.

        `seq` defaults to 0 for a fresh run_id. A run_id being *reopened*
        for another round under the same identity (see repo.reopen_run —
        a paused run resuming keeps its run_id rather than minting a new
        one) must pass its last persisted seq instead: this is a brand
        new in-memory Run object (the old one's pipeline already
        terminated and was forgotten), so without this its seq would
        restart at 0 and collide with run_events rows this same run_id
        already wrote in its earlier round(s) — see
        repo.get_last_event_seq.
        """
        if not self.is_running:
            # Queueing here would be queueing into a loop that never comes
            # round: nothing else in this class hands a run out. The caller
            # would get a handle, wait on it, and be told nothing at all —
            # which is what happened before this check existed.
            raise RuntimeError(
                f"run {run_id}: this broker is not running, so nothing would ever be "
                "dispatched — call Souk.start() (or RunBroker.start()) first"
            )
        run = Run(
            run_id=run_id,
            agent=agent,
            thread_id=thread_id,
            input_json=input_json,
            protocol=protocol,
            seq=seq,
            round_starting_seq=seq,
        )
        self._runs[run_id] = run
        self._pending_by_agent[agent].append(run_id)
        self._work_to_do.set()
        if handlers is not None:
            self._handlers[run_id] = handlers
        return run

    # ---- Handing runs to providers

    def register_provider(self, mapping: dict[AgentRef, ConnectedProvider]) -> None:
        """Which provider serves which agents, and offer them whatever is
        already waiting.

        A mapping and nothing else: registering is not an event with a
        lifecycle, it is the broker learning where an agent's work goes. A
        second entry for the same agent replaces the first, which is what a
        reconnect is.
        """
        self._providers.update(mapping)
        for provider in mapping.values():
            # A reconnect keeps the existing bucket: runs it already holds
            # are still its own, and resetting the count would let souk offer
            # past its capacity every time a connection blipped.
            self._capacity.setdefault(
                provider.public_key, _Capacity(declared=provider.max_concurrent_runs)
            )
        self._work_to_do.set()

    def serving(self, agent: AgentRef) -> ConnectedProvider | None:
        """Who is serving this agent right now, or None if nobody is.

        This is what souk means by an agent being reachable, and it is a fact
        rather than an inference. It used to be one: a provider came and asked
        for work, each ask stamped `last_seen_at`, and "asked recently" stood
        in for "still there". Nothing asks souk for anything now, so nothing
        stamps anything — an attached, healthy provider that had just finished
        a run was reported offline sixty seconds after attaching, measured.

        Node-local, and that is the known edge of it: this souk cannot see a
        provider attached to another one. Answering across processes needs a
        shared record of which node holds which connection — which multiple
        brokers need anyway, because a run created here for a provider
        attached there has to reach there. That record will also need an
        expiry, since a row saying a node serves an agent outlives the node
        being killed; `last_seen_at` becomes that expiry, written by whoever
        holds the connection instead of by the provider asking for work.
        """
        return self._providers.get(agent)

    def agents_served_by(self, public_key: str) -> list[AgentRef]:
        """Every agent this provider is currently serving here."""
        return [a for a, p in self._providers.items() if p.public_key == public_key]

    def unregister_provider(self, agents: list[AgentRef]) -> None:
        """This provider is no longer reachable. Runs it already took are
        left alone — it is still producing, and souk records no outcome it
        has not observed."""
        for agent in agents:
            self._providers.pop(agent, None)

    async def _offer_pending(self, agent: AgentRef) -> bool:
        """Offer this agent's queued runs, oldest first, until one is
        declined or the queue empties.

        **Called only from `run_forever`, and that is load-bearing.** It reads
        the head of the queue, awaits the provider, and removes it only after
        — so two of these running at once both see the same run at the head,
        both hand it over, and both then remove *a* run: the first removes the
        one they duplicated, the second removes the next one along, which is
        thereby lost. Not merely delayed. `expire_queued` only ever looks at
        the pending queue, so a run taken out of it and given to nobody is
        never offered again and never given up on either — it hangs silently,
        with its caller still watching.

        Enqueueing a run and registering a provider therefore set
        `_work_to_do` and hand out nothing themselves. The loop is waiting on
        that event when idle, so going through one door costs no latency.

        Stops at the first decline rather than trying the rest: a provider
        that just said it is full will say so again, and walking the whole
        queue to hear it once per run is work with no possible outcome.
        """
        placed = False
        while True:
            provider = self._providers.get(agent)
            queue = self._pending_by_agent.get(agent)
            if provider is None or not queue:
                return placed
            run_id = queue[0]
            run = self._runs.get(run_id)
            if run is None or run.cancel_requested:
                # Forgotten, or asked to stop before anyone took it. Drop it
                # silently, exactly as handing it out used to.
                queue.popleft()
                continue
            capacity = self._capacity.get(provider.public_key)
            if capacity is not None and not capacity.has_room:
                # souk believes this provider is full, so it offers nothing
                # until one of its runs ends — which souk sees for itself.
                # A wait for an event, not a retry.
                return placed
            if not await self._offer(run, provider):
                return placed
            queue.popleft()
            placed = True

    async def _offer(self, run: Run, provider: ConnectedProvider) -> bool:
        """One offer. True means the provider took it — from that moment the
        run has started, and this is the only place that decides so."""
        capacity = self._capacity.get(provider.public_key)
        try:
            async with asyncio.timeout(self.deliver_timeout_seconds):
                accepted = await provider.deliver(
                    ClaimedRun(
                        run_id=run.run_id,
                        agent=run.agent,
                        thread_id=run.thread_id,
                        run_input=run.input_json,
                    )
                )
        except TimeoutError:
            # It said nothing. The run stays queued and will be offered
            # again, which is all souk can do — and is why this is counted
            # rather than retried quietly: if the provider did take it,
            # offering again runs it twice.
            if capacity is not None:
                capacity.unanswered += 1
            logger.warning(
                "provider %s did not answer an offer of run %s within %ss (%d so far)",
                provider.public_key[:16],
                run.run_id,
                self.deliver_timeout_seconds,
                capacity.unanswered if capacity else 0,
            )
            return False
        except Exception:
            if capacity is not None:
                capacity.unanswered += 1
            logger.exception("run %s: delivering to its provider failed", run.run_id)
            return False
        if not accepted:
            if capacity is not None and capacity.has_room:
                # Declined while souk believed it had room: what it declared
                # and what it does disagree. Believe the provider, which is
                # the one that knows, and record that souk had to find out by
                # being refused.
                capacity.misdeclared += 1
                capacity.in_flight = capacity.declared or capacity.in_flight
                logger.warning(
                    "provider %s declined a run while souk believed it had room "
                    "(now %d/%s in flight); treating it as full",
                    provider.public_key[:16],
                    capacity.in_flight,
                    capacity.declared,
                )
            return False
        # Taken. Recorded and marked in one step, with no await in between,
        # so nothing can observe a run that has been handed over and belongs
        # to nobody.
        run.claimed_by = provider.public_key
        run.cancel_notify = provider.cancel
        if capacity is not None:
            capacity.in_flight += 1
        # The run's own task starts here, not at enqueue: until a provider
        # took it there was nothing for a pipeline to do, and one waiting on
        # an empty queue for a run nobody wanted is a task per queued run.
        handlers = self._handlers.get(run.run_id)
        if handlers is not None:
            self._spawn(_pipeline(run, handlers, self), name=f"pipeline:{run.run_id}")
        run.in_queue.put_nowait(Claim())
        return True

    def _spawn_unsupervised(self, coro, *, name: str | None = None) -> asyncio.Task:
        # asyncio.create_task() doesn't itself keep the task alive — nothing
        # else in this process holds a reference to a run's pipeline task
        # otherwise, which makes it a candidate for garbage collection
        # mid-run (a real, not hypothetical, failure mode: this is exactly
        # what silently killed it under test). _pipeline_tasks is that
        # reference; the done-callback is just bookkeeping so the set
        # doesn't grow unbounded.
        task = asyncio.create_task(coro, name=name)
        self._pipeline_tasks.add(task)
        task.add_done_callback(self._pipeline_tasks.discard)
        return task

    def get(self, run_id: str) -> RunSnapshot | None:
        """What this run currently looks like, as a copy. None if this broker
        is not dispatching it — finished, cancelled, or never here."""
        run = self._runs.get(run_id)
        return _snapshot(run) if run is not None else None

    def push(self, run_id: str, command: Command) -> bool:
        """Affect a run: the only way in from outside. False if the run is
        not being dispatched here, which is ordinary rather than an error —
        a straggler for a run that already ended.

        Everything that wants to change a run comes through here: a worker
        reporting an event or the end of its stream (see souk.core), the
        health sweep giving up on one (see souk.health). They used to reach
        into `run.in_queue` themselves, which meant holding the live object.
        """
        run = self._runs.get(run_id)
        if run is None:
            return False
        if isinstance(command, Fail) and run.claimed_by is not None:
            # A provider took this run and then neither finished it nor
            # reported it failed — the health sweep had to give up, and it
            # held a place in that provider's bucket the whole time. souk saw
            # both halves, so it can say so.
            capacity = self._capacity.get(run.claimed_by)
            if capacity is not None:
                capacity.abandoned += 1
                logger.warning(
                    "provider %s abandoned run %s (%d so far): took it and never ended it",
                    run.claimed_by[:16],
                    run_id,
                    capacity.abandoned,
                )
        run.in_queue.put_nowait(command)
        return True

    def subscribe(self, run_id: str) -> AsyncIterator[Any]:
        """This run's events, in order, until its stream ends. Empty if this
        broker is not dispatching it.

        The read side of the same encapsulation: draining needed the live
        object, this needs an id. A distributed broker answers it with a
        subscription of its own rather than a local queue.
        """
        run = self._runs.get(run_id)
        return _drain_run(run) if run is not None else _no_events()

    def request_cancel(self, run_id: str) -> bool:
        """Ask for a run to stop. False if this broker is not dispatching it.

        Which of the two situations it is, is decided here rather than later,
        because the broker is what knows: it either handed this run to a
        provider or it did not.

        **A provider has it.** The request goes onto the run's own queue, in
        order behind everything else about it, and its pipeline asks the
        provider to stop and records `cancelling`. souk can ask; it cannot
        compel, so the outcome is still decided when the stream ends.

        **Nobody has it.** There is no pipeline — one starts when a provider
        takes a run — so the broker spawns a single handler run to record
        `cancelled` and nothing else. It also drops out of the pending queue
        (see `_offer_pending`, which skips a run once `cancel_requested` is
        set), so it is never offered to anyone.

        `cancel_requested` is set synchronously, before either path, so
        nothing can offer this run in the meantime.
        """
        run = self._runs.get(run_id)
        if run is None:
            return False
        run.cancel_requested = True
        if run.claimed_by is not None:
            run.in_queue.put_nowait(RequestCancel())
            return True
        self._spawn(self._cancel_queued(run), name=f"cancel:{run_id}")
        return True

    def expire_queued(self, timeout_seconds: float) -> list[str]:
        """Give up on runs nobody has taken in time.

        The broker's own question — which runs am I still holding, and how
        long have I held them — and it answers both from memory: `queued_at`
        does not change, so there is nothing to read.

        Each one gets a single handler run to record the failure, the same
        shape as a cancel arriving for a queued run: no pipeline, because a
        pipeline starts when a provider takes a run.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
        expired: list[str] = []
        for queue in list(self._pending_by_agent.values()):
            for run_id in list(queue):
                run = self._runs.get(run_id)
                if run is None or run.queued_at > cutoff:
                    continue
                queue.remove(run_id)
                expired.append(run_id)
                self._spawn(
                    self._one_shot(run, Fail("no_provider_took_it")),
                    name=f"expire:{run_id}",
                )
        return expired

    async def _cancel_queued(self, run: Run) -> None:
        """One handler run, for a run nobody took.

        The broker holds no database. Recording `cancelled` is a handler's
        job like every other write about a run, so it runs one — there is
        simply no pipeline to put it on.
        """
        await self._one_shot(run, RequestCancel())

    async def _one_shot(self, run: Run, command: Command) -> None:
        """Run exactly one handler for a run that has no pipeline, then end it.

        The broker holds no database, so recording what just happened is a
        handler's job like every other write about a run. What it does not
        need is a pipeline: that exists to order many commands against one
        run, and a run nobody took gets exactly one.
        """
        handler = (self._handlers.get(run.run_id) or {}).get(type(command))
        if handler is not None:
            try:
                await handler(run, command)
            except Exception:
                logger.exception(
                    "run %s: recording %s failed", run.run_id, type(command).__name__
                )
        run.out_queue.put_nowait(END_OF_STREAM)
        self.forget(run.run_id)

    def active_run_ids(self) -> list[str]:
        """Every run currently in dispatch. Live in-memory state, distinct
        from the database's view — which also holds runs that already
        finished."""
        return list(self._runs)

    def accept_late_ack(self, run_id: str, claimed_by: str) -> bool:
        """A provider is producing for a run souk does not think it took.

        The events are the proof — nothing else could produce them — so this
        is the ack, arriving after souk stopped waiting. See
        `_Capacity.answered_late` for why that is the provider's doing rather
        than the network's.

        Accepted only from the provider actually registered for that run's
        agent. Otherwise it would be a way to take over anyone's run by
        guessing a run_id, which is what the ownership check on report_event
        exists to stop.

        False if this is not that, and the caller then rejects the events —
        which is what it did before there was any way to be right here.
        """
        run = self._runs.get(run_id)
        if run is None or run.claimed_by is not None:
            return False
        provider = self._providers.get(run.agent)
        if provider is None or provider.public_key != claimed_by:
            return False

        # Out of the queue, or the loop offers it again to the very provider
        # already running it.
        queue = self._pending_by_agent.get(run.agent)
        if queue is not None and run_id in queue:
            queue.remove(run_id)

        capacity = self._capacity.get(claimed_by)
        if capacity is not None:
            capacity.answered_late += 1
            capacity.in_flight += 1
        logger.warning(
            "provider %s answered late for run %s (%d so far): already producing for "
            "a run souk had put back in the queue",
            claimed_by[:16],
            run_id,
            capacity.answered_late if capacity else 0,
        )
        run.claimed_by = claimed_by
        run.cancel_notify = provider.cancel
        handlers = self._handlers.get(run_id)
        if handlers is not None:
            self._spawn(_pipeline(run, handlers, self), name=f"pipeline:{run_id}")
        run.in_queue.put_nowait(Claim())
        return True

    def quality(self) -> dict[str, ProviderQuality]:
        """What souk has observed about each provider, by public key.

        A snapshot: reading it cannot change it, and holding it will not keep
        it current — ask again.
        """
        return {
            key: ProviderQuality(
                in_flight=c.in_flight,
                declared=c.declared,
                misdeclared=c.misdeclared,
                abandoned=c.abandoned,
                unanswered=c.unanswered,
                answered_late=c.answered_late,
            )
            for key, c in self._capacity.items()
        }

    def forget(self, run_id: str) -> None:
        """This run is over, however it ended.

        Returning its place in the provider's bucket happens here rather than
        somewhere more specific because *every* ending passes through here —
        finished, failed, cancelled, reaped. A place returned on only some of
        those endings is a bucket that empties permanently on the rest.
        """
        run = self._runs.pop(run_id, None)
        self._handlers.pop(run_id, None)
        if run is not None and run.claimed_by is not None:
            capacity = self._capacity.get(run.claimed_by)
            if capacity is not None and capacity.in_flight > 0:
                capacity.in_flight -= 1
                # A place came back, so there may now be somewhere to put
                # something. This is what lets the loop wait instead of ask.
                self._work_to_do.set()
