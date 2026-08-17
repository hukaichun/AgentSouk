"""The provider's own loop: take the runs handed over, run them, push their
events back.

Two queues and one loop between them, which is the whole shape:

    deliver() ──▶ job queue ──▶ loop ──▶ output queue ──▶ on_event/on_finish
                      │                       │
              full ⇒ declined           one event per chunk

**Work is handed over; this loop does not ask for it.** Whoever is serving
this provider offers each run, and `deliver` is where that offer lands.
Returning True is the ack — the run counts as started from that moment.
Returning False leaves it with the caller for someone to take later, so
declining is this provider's way of saying it is full, and the job queue
being full is what says it.

That is the only channel capacity has. The other side cannot see how much a
provider can take, and does not try: it offers, and a provider that cannot
take more says no.

**The loop is this provider's, and everything in it is this provider's
policy** — how deep the job queue is, how many runs to have going at once,
what to do when an agent raises. souk shipped one of these once and it took
those decisions with it.

**This module names nothing on the other side.** It used to hold a `souk`
and call `souk.report_event(...)` / `souk.finish_run(...)`, and read
`run.run_id` / `run.agent.name` / `run.run_input` off souk's own object —
souk's method names, argument order and model fields were all part of this
package's interface without either side declaring it. Now runs arrive as
`DeliveredRun` and results leave through two callables the caller supplies.
Whoever wires the two together is the one place entitled to know both
shapes; see `contract.py`.

What stays here is what belongs to a loop: ordering. `_report_output` is a
single consumer, so a run's events leave in the order the agent produced
them — a guarantee that would evaporate if this handed out a stream for
callers to drain however they liked.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from souk_provider_sdk.identity import ProviderIdentity
from souk_provider_sdk.provider import DeliveredRun, Provider

logger = logging.getLogger("souk_provider_sdk.runtime")


class ProviderRuntime:
    """One of these per provider, never per agent: its capacity is a budget
    across every agent it serves, exactly as one process is.

    `public_key`, `deliver` and `cancel` are what a caller needs to hand this
    thing work — souk's `ConnectedProvider` is that same trio, so an adapter
    that converts souk's delivered run into a `DeliveredRun` is the whole of
    what standing this in front of a souk takes.

    `on_event(run_id, event)` and `on_finish(run_id)` are where results go.
    Both are called from this runtime's own task, in production order, and
    both are synchronous on purpose: an agent must never be made to wait on
    whatever is on the other end, and `on_finish` is called while unwinding a
    cancellation, where an await would be interrupted before it arrived.
    """

    def __init__(
        self,
        identity: ProviderIdentity,
        provider: Provider,
        *,
        on_event: Callable[[str, Any], Any] | None = None,
        on_finish: Callable[[str], Any] | None = None,
        max_queued_runs: int = 1,
        max_concurrent_runs: int | None = None,
    ) -> None:
        self.identity = identity
        self.provider = provider
        self.on_event = on_event
        self.on_finish = on_finish
        # How many runs may be waiting to start. This is what souk sees as
        # capacity: when it is full, `deliver` declines and the run stays
        # souk's problem rather than becoming a backlog nobody can see.
        #
        # Small on purpose. A deep queue looks like throughput and is really
        # a promise this provider has not checked it can keep — souk would
        # believe every one of those runs had started.
        self._jobs: asyncio.Queue = asyncio.Queue(maxsize=max_queued_runs)
        # Events on their way back. Separate from running them so an agent
        # producing fast is never blocked by a slow report — over a wire that
        # is a socket write, and the alternative is an agent whose speed
        # depends on the network.
        self._output: asyncio.Queue = asyncio.Queue()
        self.max_concurrent_runs = max_concurrent_runs
        self._in_flight: dict[str, asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()
        self._running = False

    @property
    def public_key(self) -> str:
        """Who this provider is. From its keypair — it holds the private
        half, so there is nothing to look up and nothing to be told."""
        return self.identity.public_key

    # ---- What the other side calls

    async def deliver(self, run: DeliveredRun) -> bool:
        """This run is being offered. Take it, or say no.

        True means it is accepted and will be started, and the caller may
        record it as running from here. False means it was not taken and the
        caller keeps it — the only honest answer when this provider is full,
        and the only way it has of saying so.

        `run` is a `DeliveredRun`, this package's own type. Converting from
        whatever the other side sends is the caller's job, deliberately: this
        loop reading another codebase's model is how it broke before.
        """
        if not self._running:
            return False
        if self.max_concurrent_runs is not None and len(self._in_flight) >= self.max_concurrent_runs:
            return False
        try:
            self._jobs.put_nowait(run)
        except asyncio.QueueFull:
            return False
        return True

    def cancel(self, run_id: str) -> None:
        """The other side is asking for a run to stop.

        A request, and complying is this provider's choice: the asker
        publishes it and then waits to see what the stream does. One that
        ignores it and finishes has finished, and that is what gets recorded.

        This one complies, by cancelling the task running the agent — the
        only way to interrupt an arbitrary async generator.
        """
        task = self._in_flight.get(run_id)
        if task is not None:
            task.cancel()

    # ---- Lifecycle

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._spawn(self._run_jobs(), name="provider-jobs")
        self._spawn(self._report_output(), name="provider-output")

    async def aclose(self, *, cancel_in_flight: bool = False) -> None:
        """Stop taking work, then wait for what is already going.

        `cancel_in_flight` picks the shutdown: draining lets the other side
        see each run's real outcome, cancelling makes each stream end without
        a RUN_FINISHED, which souk records as failed unless it had already
        asked for a stop. Neither is a lie — they are different shutdowns.

        Closing says nothing to whoever is serving this provider. Going off
        their roster is a separate call on their side, and forgetting it
        leaves these agents looking reachable until a liveness sweep notices.
        """
        self._running = False
        if cancel_in_flight:
            for task in list(self._in_flight.values()):
                task.cancel()
        for task in list(self._tasks):
            if task.get_name() in ("provider-jobs", "provider-output"):
                task.cancel()
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def _spawn(self, coro, *, name: str) -> asyncio.Task:
        # The loop keeps only a weak reference to a running task, so one
        # nothing else holds can be collected mid-flight.
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ---- The loop

    async def _run_jobs(self) -> None:
        """Take runs off the job queue and start them.

        One task per run rather than one at a time: a provider serving
        several agents is the ordinary case, and a slow one must not hold up
        the rest. How many at once is `max_concurrent_runs`, enforced where
        the other side can see it — in `deliver`, by declining.
        """
        while True:
            run = await self._jobs.get()
            task = self._spawn(self._execute(run), name=f"run:{run.run_id}")
            self._in_flight[run.run_id] = task
            task.add_done_callback(
                lambda _t, run_id=run.run_id: self._in_flight.pop(run_id, None)
            )

    async def _execute(self, run: DeliveredRun) -> None:
        """One run, start to finish, its output queued as it comes.

        The end-of-stream marker is queued in a `finally` because it is the
        only thing that ends the run for the other side: however this stops —
        finishing, raising, or being cancelled — the outcome is decided from
        what was seen, and nothing can be decided until the stream is known
        to be over.
        """
        name = run.agent_name
        try:
            async for event in self.provider.run_stream(name, run.run_input):
                self._output.put_nowait((run.run_id, event))
        except asyncio.CancelledError:
            logger.info("run %s: agent stopped", run.run_id)
            raise
        except Exception:
            logger.exception("run %s: agent failed", run.run_id)
        finally:
            self._output.put_nowait((run.run_id, _END))

    async def _report_output(self) -> None:
        """Drain the output queue through the callbacks, in order.

        **One consumer, and that is the point.** A run's events leave in the
        order the agent produced them because exactly one task takes them off
        this queue. Handing callers a stream to drain themselves would make
        that ordering theirs to preserve, and nothing would go red when two
        of their tasks raced — so the guarantee stays here, which is the one
        thing this loop owes anybody.

        Both callbacks are synchronous by design: an agent must never be made
        to wait on whatever is downstream, and the end marker is sent while
        unwinding a cancellation, where an await would be interrupted before
        it ever arrived.

        A callback that raises is logged and swallowed. It belongs to the
        caller, and one bad send must not kill the consumer that every other
        run's ordering depends on.
        """
        while True:
            run_id, event = await self._output.get()
            try:
                if event is _END:
                    if self.on_finish is not None:
                        self.on_finish(run_id)
                elif self.on_event is not None:
                    self.on_event(run_id, event)
            except Exception:
                logger.exception("run %s: reporting failed", run_id)


# Queued after a run's last event. Not an AG-UI event and never passed to
# `on_event` — ending a run is `on_finish`, a different call, and this only
# says which of the two to make.
_END = object()
