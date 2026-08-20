"""The provider's own loop: take the runs souk hands over, run them, push
their events back.

Two queues and one loop between them, which is the whole shape:

    souk ──deliver()──▶ job queue ──▶ loop ──▶ output queue ──▶ souk
                            │                      │
                    full ⇒ declined          one event per chunk

**souk hands work over; it does not ask for it.** The broker finds whoever
serves an agent and offers each run, and `deliver` is where that offer lands.
Returning True is the ack: from that moment souk records the run as started.
Returning False leaves it queued for someone to take later — so declining is
this provider's way of saying it is full, and the job queue being full is what
says it.

That is the only channel capacity has. souk cannot see how much a provider can
take, and does not try: it offers, and a provider that cannot take more says
no.

**The loop is this provider's, and everything in it is this provider's
policy** — how deep the job queue is, how many runs to have going at once,
what to do when an agent raises. souk shipped one of these once and it took
those decisions with it.

Nothing here imports souk. `souk` below is anything with `report_event` and
`finish_run` (see `SoukConnection`); a `Souk` object satisfies it structurally
in-process, and a remote binding carries the same two calls over a wire.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from souk_provider_sdk.identity import ProviderIdentity
from souk_provider_sdk.provider import Provider

logger = logging.getLogger("souk_provider_sdk.runtime")


class ProviderRuntime:
    """One of these per provider, never per agent: its capacity is a budget
    across every agent it serves, exactly as one process is.

    Satisfies souk's `ConnectedProvider` — `public_key`, `deliver`, `cancel`
    — which is the whole of what the broker needs to know about anybody.
    """

    def __init__(
        self,
        identity: ProviderIdentity,
        provider: Provider,
        souk: Any,
        *,
        max_queued_runs: int = 1,
        max_concurrent_runs: int | None = None,
    ) -> None:
        self.identity = identity
        self.provider = provider
        self.souk = souk
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

    # ---- What souk calls

    async def deliver(self, run: Any) -> bool:
        """souk is offering this run. Take it, or say no.

        True means it is accepted and will be started; souk records the run
        as running from here. False means it was not taken and souk should
        keep it — the only honest answer when this provider is full, and the
        only way it has of saying so.
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
        """souk is asking for a run to stop.

        A request, and complying is this provider's choice: souk publishes it
        and then waits to see what the stream does. One that ignores it and
        finishes has finished, and souk records that.

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

        `cancel_in_flight` picks the shutdown: draining lets souk see each
        run's real outcome, cancelling makes each stream end without a
        RUN_FINISHED, which souk records as failed unless it had already
        asked for a stop. Neither is a lie — they are different shutdowns.
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
        souk can see it — in `deliver`, by declining.
        """
        while True:
            run = await self._jobs.get()
            task = self._spawn(self._execute(run), name=f"run:{run.run_id}")
            self._in_flight[run.run_id] = task
            task.add_done_callback(
                lambda _t, run_id=run.run_id: self._in_flight.pop(run_id, None)
            )

    async def _execute(self, run: Any) -> None:
        """One run, start to finish, its output queued as it comes.

        The end-of-stream marker is queued in a `finally` because it is the
        only thing that ends the run for souk: however this stops — finishing,
        raising, or being cancelled — souk decides the outcome from what it
        saw, and can decide nothing until it knows the stream is over.
        """
        name = run.agent.name
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
        """Drain the output queue into souk, in order.

        One consumer, so a run's events reach souk in the order the agent
        produced them. Reporting is synchronous on souk's side by design — a
        provider must never wait on souk's persistence, and the end marker is
        sent while unwinding a cancellation, where an await would be
        interrupted before it ever arrived.
        """
        while True:
            run_id, event = await self._output.get()
            try:
                if event is _END:
                    self.souk.finish_run(run_id, claimed_by=self.public_key)
                else:
                    self.souk.report_event(run_id, event, claimed_by=self.public_key)
            except Exception:
                logger.exception("run %s: reporting to souk failed", run_id)


# Queued after a run's last event. Not an AG-UI event and never relayed as
# one — souk's `finish_run` is a different call, and this only says which of
# the two to make.
_END = object()
