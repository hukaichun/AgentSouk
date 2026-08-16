"""In-memory hand-off between a run's caller and the worker holding it.

Neither side is named by transport here, deliberately. A caller reaches a
run through a protocol adapter, and a worker reaches it through core's three
methods (`claim_work` / `report_event` / `finish_run`); what carried either
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
import logging
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from souk.models import AgentRef

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
    # whoever claimed it (souk.core.claim_work's `on_cancel`), because
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
    def __init__(self, spawn=None) -> None:
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
        # Lets a long-polling claim block until a run actually
        # shows up for one of its agents instead of sleeping through a
        # fixed poll interval — see core.claim_work. A plain asyncio.Event
        # rather than anything shaped like a wire frame, so this stays
        # a swap-in seam for a distributed backend (e.g. Postgres
        # LISTEN/NOTIFY) if souk is ever split across multiple processes;
        # nothing above this depends on wakes being in-process.
        self._wake_subscribers: dict[AgentRef, set[asyncio.Event]] = defaultdict(set)
        self._pipeline_tasks: set[asyncio.Task] = set()

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
        for event in self._wake_subscribers.get(agent, ()):
            event.set()
        if handlers is not None:
            self._spawn(_pipeline(run, handlers, self), name=f"pipeline:{run_id}")
        return run

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

    def subscribe_wake(self, agents: list[AgentRef]) -> asyncio.Event:
        event = asyncio.Event()
        for agent in agents:
            self._wake_subscribers[agent].add(event)
        return event

    def unsubscribe_wake(self, agents: list[AgentRef], event: asyncio.Event) -> None:
        for agent in agents:
            self._wake_subscribers[agent].discard(event)

    def claim(
        self,
        agents: list[AgentRef],
        *,
        claimed_by: str,
        cancel_notify: Callable[[str], None] | None = None,
        max_claim: int | None = None,
    ) -> list[Run]:
        """Hands up to `max_claim` runs across `agents` to one worker,
        round-robining between agents so a cap doesn't starve later ids in
        the list.

        Claiming and being claimed are one act, not two. Each run returned
        is marked as `claimed_by` this worker and has its Claim queued
        *before this returns*, with no `await` anywhere in between — so
        there is no window in which a run has been handed out but souk
        still believes nobody has it. That window used to exist (poll
        returned run_ids, the provider came back later to claim them) and
        needed a second cancelled-check to cover it.

        `max_claim=None` means the caller never reported a capacity at
        all — drains everything currently queued, unlimited (the old
        all-at-once behavior). `max_claim=0` is different: it means the
        caller explicitly reported "no spare capacity right now", so
        nothing is claimed — distinct from None, not a stand-in for it.

        Filters out runs already asked to stop (see Run.cancel_requested)
        rather than ever handing one over — dropped silently here, not
        counted against max_claim, same as an already-forgotten run_id.
        """
        if max_claim is not None and max_claim <= 0:
            return []

        found: list[Run] = []
        queues = [self._pending_by_agent.get(agent) for agent in agents]
        while any(queues):
            if max_claim is not None and len(found) >= max_claim:
                break
            progressed = False
            for queue in queues:
                if not queue:
                    continue
                if max_claim is not None and len(found) >= max_claim:
                    break
                run_id = queue.popleft()
                progressed = True
                run = self._runs.get(run_id)
                if run is not None and not run.cancel_requested:
                    run.claimed_by = claimed_by
                    run.cancel_notify = cancel_notify
                    run.in_queue.put_nowait(Claim())
                    found.append(run)
            if not progressed:
                break
        return found

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
        """Ask for a run to stop — see the module docstring for the two
        halves this splits into. False if this broker is not dispatching
        it."""
        run = self._runs.get(run_id)
        if run is None:
            return False
        _request_cancel(run)
        return True

    def active_run_ids(self) -> list[str]:
        """Every run currently in dispatch. Live in-memory state, distinct
        from the database's view — which also holds runs that already
        finished."""
        return list(self._runs)

    def forget(self, run_id: str) -> None:
        self._runs.pop(run_id, None)
