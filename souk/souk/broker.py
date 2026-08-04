"""In-memory hand-off between the HTTP gateway and the gRPC relay.

Each run is modeled like a tiny OS process: a `Run` is pure data (no
methods — see its docstring), with two queues attached to it playing the
role of stdin/stdout:

- `in_queue` ("stdin"): every external actor that wants to *affect* a run
  — the gRPC side relaying frames from the agent (see grpc_server.py's
  AgentSession), an explicit A2A tasks/cancel, the health sweep giving up
  on a stalled/abandoned run — does so by pushing one `Command` here.
  Nobody else ever touches a Run's fields directly. An HTTP/SSE consumer
  disconnecting is *not* one of these: souk's own DB state is
  authoritative regardless of whether anyone's still watching a
  particular stream, so a dropped connection alone never cancels a run —
  see api_agui.run_agent's event_stream for the reasoning.
- `out_queue` ("stdout"): AG-UI events the run emits, drained by whichever
  HTTP/SSE consumer (api_agui.py / api_a2a.py) is watching it.

Exactly one task per run (`_pipeline`, spawned by `enqueue_run`) reads
`in_queue` and is the *only* code that ever mutates a Run's fields —
dispatching each command to a handler function ("function objects", see
grpc_server.py's HANDLERS) supplied by the caller — with one deliberate
exception: `Run.cancelled` (see its own docstring below). Everything
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

souk runs as a single process holding both the HTTP server and the gRPC
server on one event loop, so all of this is implemented with plain
asyncio primitives rather than round-tripping through Postgres. Postgres
(souk/db.py) is the durable record for anything that needs to survive a
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

logger = logging.getLogger("souk.broker")

# Sentinel put on a run's out_queue to signal the stream has ended.
END_OF_STREAM = object()


# ---- Commands: pure data, pushed onto a run's in_queue. What each one
# means and who's allowed to handle it lives in whoever supplies the
# HandlerMap (see grpc_server.HANDLERS) — broker.py only needs to know
# these exist, not what they do.


@dataclass
class Claim:
    """An agent has claimed this run and is ready to receive its input —
    carries the specific AgentSession connection's outbound queue that
    owns it from here on (see grpc_server._handle_claim).
    """

    outbound: asyncio.Queue[Any]


@dataclass
class RelayEvent:
    """The agent relayed one AG-UI event JSON for this run."""

    json_payload: str


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
    grpc_server._handle_cancel on the run's own pipeline task. Pushed by
    `request_cancel` below, never directly — see that function for the
    synchronous half (`Run.cancelled`) this doesn't cover.
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
    module's own RunBroker (it only ever reads run_id/agent_id to route,
    never mutates) — except `cancelled`, see its own docstring just below.
    """

    run_id: str
    agent_id: str
    thread_id: str
    input_json: dict[str, Any]
    protocol: str  # "ag-ui" | "a2a"
    seq: int = 0
    pause_payload: dict[str, Any] | None = None
    agent_outbound: asyncio.Queue[Any] | None = None
    # The one field anyone may set directly, synchronously, from any
    # thread of control — see request_cancel() below. Everything that
    # reacts to a run being cancelled reads this rather than waiting on
    # RequestCancel to reach the front of in_queue:
    #   - broker.poll() won't hand an already-cancelled run to an agent
    #     in the first place.
    #   - grpc_server._handle_claim double-checks it for the narrow race
    #     poll() can't close (already handed out, cancelled before the
    #     agent's claim envelope arrives) and bounces a cancel signal
    #     back immediately instead of delivering input nobody wants.
    #   - grpc_server._handle_finish checks it so the agent's own
    #     end_of_stream (sent once it unwinds from the cancel) doesn't
    #     overwrite the "cancelled" DB status back to "completed".
    # That last one *was* a real race in an earlier version of this
    # (_handle_cancel and _handle_finish running concurrently) — not
    # anymore: both are only ever invoked from this run's own pipeline
    # task, serialized like every other command, so by the time
    # _handle_finish runs, _handle_cancel (if it ran at all) already has.
    cancelled: bool = False
    in_queue: asyncio.Queue[Command] = field(default_factory=asyncio.Queue)
    out_queue: asyncio.Queue[Any] = field(default_factory=asyncio.Queue)


def request_cancel(run: Run) -> None:
    """The one correct way to cancel a run — call this, don't push
    RequestCancel directly. Splits into exactly the two halves described
    on `Run.cancelled` and `RequestCancel`: flips the flag immediately so
    the rest of the system (chiefly broker.poll(), and
    grpc_server._handle_claim's narrower fallback check for the case
    poll() already handed the run out) can react without delay, then
    queues the actual DB write / agent notification for the run's own
    pipeline task to carry out in order relative to its other commands.

    Cancelling a run is always an explicit act — see api_agui.run_agent's
    event_stream for why an HTTP/SSE consumer disconnecting must never
    call this. In practice that leaves A2A's tasks/cancel as the only
    caller, an ordinary request context, not one already being torn
    down — but both operations here are synchronous regardless (neither
    is an `await`), so this would still be safe to call from a context
    that was mid-teardown, if one ever needs to again.
    """
    run.cancelled = True
    run.in_queue.put_nowait(RequestCancel())


async def drain_run(run: Run) -> AsyncIterator[Any]:
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


HandlerMap = dict[type, Callable[[Run, Any], Awaitable[None]]]


async def _pipeline(run: Run, handlers: HandlerMap, owner: "RunBroker") -> None:
    """The run's own single-consumer task — the only thing that ever
    dispatches a Command to a handler, so no two handlers ever run
    concurrently against the same Run. Terminates (and forgets the run
    from the registry) on:
    - FinishStream — the agent's own authoritative "done" signal.
    - Fail — the health sweep gave up on this run outright (see
      grpc_server._handle_fail); no FinishStream is ever coming for one
      of these either.
    - RequestCancel, but only if the run was never claimed
      (`agent_outbound is None`) — an explicit tasks/cancel (the only
      thing that ever pushes this; see broker.request_cancel) arriving
      before any agent claimed the run means no FinishStream will ever
      come for it. If it *was* already claimed, this does not terminate
      the pipeline by itself — it waits for the agent's own FinishStream
      once it unwinds after being told to stop (see
      grpc_server._handle_cancel's docstring for the narrow late-claim
      race this leaves to _handle_claim's own check instead).
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
        if isinstance(cmd, RequestCancel) and run.agent_outbound is None:
            break
    owner.forget(run.run_id)


class RunBroker:
    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}
        self._pending_by_agent: dict[str, deque[str]] = defaultdict(deque)
        # Lets a long-polling PollForWork call block until a run actually
        # shows up for one of its agent_ids instead of sleeping through a
        # fixed poll interval — see grpc_server.PollForWork. Plain
        # asyncio.Event rather than anything grpc/pb2-shaped, so this stays
        # a swap-in seam for a distributed backend (e.g. Postgres
        # LISTEN/NOTIFY) if souk is ever split across multiple processes;
        # nothing above this depends on wakes being in-process.
        self._wake_subscribers: dict[str, set[asyncio.Event]] = defaultdict(set)
        self._pipeline_tasks: set[asyncio.Task] = set()

    def enqueue_run(
        self,
        run_id: str,
        agent_id: str,
        thread_id: str,
        input_json: dict[str, Any],
        protocol: str,
        handlers: HandlerMap | None = None,
        seq: int = 0,
    ) -> Run:
        """`handlers=None` skips spawning this run's pipeline task — only
        useful for tests exercising pure registry/poll/wake logic in
        isolation. Every real caller (api_agui.py, api_a2a.py,
        grpc_server.py's auto-resume path) must pass grpc_server.HANDLERS,
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
            agent_id=agent_id,
            thread_id=thread_id,
            input_json=input_json,
            protocol=protocol,
            seq=seq,
        )
        self._runs[run_id] = run
        self._pending_by_agent[agent_id].append(run_id)
        for event in self._wake_subscribers.get(agent_id, ()):
            event.set()
        if handlers is not None:
            # asyncio.create_task() doesn't itself keep the task alive —
            # nothing else in this process holds a reference to a run's
            # pipeline task otherwise, which makes it a candidate for
            # garbage collection mid-run (a real, not hypothetical,
            # failure mode: this is exactly what silently killed it under
            # test). _pipeline_tasks is that reference; the done-callback
            # is just bookkeeping so the set doesn't grow unbounded.
            task = asyncio.create_task(_pipeline(run, handlers, self))
            self._pipeline_tasks.add(task)
            task.add_done_callback(self._pipeline_tasks.discard)
        return run

    def subscribe_wake(self, agent_ids: list[str]) -> asyncio.Event:
        event = asyncio.Event()
        for agent_id in agent_ids:
            self._wake_subscribers[agent_id].add(event)
        return event

    def unsubscribe_wake(self, agent_ids: list[str], event: asyncio.Event) -> None:
        for agent_id in agent_ids:
            self._wake_subscribers[agent_id].discard(event)

    def poll(self, agent_ids: list[str], max_claim: int | None = None) -> list[Run]:
        """Returns (and removes from the pending queue) up to `max_claim`
        runs across `agent_ids`, round-robining between agents so a cap
        doesn't starve later ids in the list.

        `max_claim=None` means the caller never reported a capacity at
        all — drains everything currently queued, unlimited (the old
        all-at-once behavior). `max_claim=0` is different: it means the
        caller explicitly reported "no spare capacity right now", so
        nothing is claimed — distinct from None, not a stand-in for it.

        Filters out already-cancelled runs (see Run.cancelled) rather
        than ever handing one to an agent — dropped silently here, not
        counted against max_claim, same as an already-forgotten run_id.
        This is what actually closes the "cancelled before ever being
        claimed" gap in the common case: grpc_server._handle_claim's own
        cancelled check exists only for the much narrower race this
        can't see (already returned by this call, cancelled before the
        agent's claim envelope arrives).
        """
        if max_claim is not None and max_claim <= 0:
            return []

        found: list[Run] = []
        queues = [self._pending_by_agent.get(agent_id) for agent_id in agent_ids]
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
                if run is not None and not run.cancelled:
                    found.append(run)
            if not progressed:
                break
        return found

    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def forget(self, run_id: str) -> None:
        self._runs.pop(run_id, None)


broker = RunBroker()
