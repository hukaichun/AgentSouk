from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from funduq_provider_sdk.identity import ProviderIdentity
from funduq_provider_sdk.provider import DeliveredRun, Provider

if TYPE_CHECKING:
    from funduq_provider_sdk.link import FunduqLink

logger = logging.getLogger("funduq_provider_sdk.runtime")


class ProviderRuntime:
    """Runs a `Provider`'s agents locally: queues delivered runs, executes each as a task, and streams
    resulting events back through `link` (dropping them if no link is attached)."""

    def __init__(
        self,
        identity: ProviderIdentity,
        provider: Provider,
        *,
        max_queued_runs: int = 1,
        max_concurrent_runs: int | None = None,
    ) -> None:
        self.identity = identity
        self.provider = provider
        self.link: "FunduqLink | None" = None
        # Claiming no limit (max_concurrent_runs=None, the default) is a
        # declaration funduq takes at its word: a decline from an unlimited
        # provider is abnormal behaviour, counted against it, and stops
        # offers until it acts. So an unlimited runtime must never decline
        # by accident — its intake queue is unbounded, and pacing (if the
        # author wants any) is declared via max_concurrent_runs instead.
        self._jobs: asyncio.Queue = asyncio.Queue(
            maxsize=max_queued_runs if max_concurrent_runs is not None else 0
        )
        self._output: asyncio.Queue = asyncio.Queue()
        self.max_concurrent_runs = max_concurrent_runs
        self._in_flight: dict[str, asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()
        self._running = False

    @property
    def public_key(self) -> str:
        return self.identity.public_key


    async def deliver(self, run: DeliveredRun) -> bool:
        """Queues `run` for execution; returns False (without queuing) if not started, at `max_concurrent_runs`, or the queue is full.

        Every accepted run goes to the agent callable as it arrives — the
        runtime imposes no ordering of its own. A run whose
        `forwardedProps.addressedRunId` names another run is a declared
        *interjection*: the caller asks to join that run's turn in flight
        (distinct from `parentRunId`, which is plain continuation). Whether
        and how to honour it — absorb it into the named turn, treat it as
        the next turn, ignore it — is the agent author's decision, made in
        the agent's own code against its own live loop.
        `serialize_per_thread` is an off-the-shelf wrapper for authors who
        want one-turn-at-a-time per thread.
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
        """Cancels the asyncio task executing `run_id`, if it is currently in flight; a no-op otherwise."""
        task = self._in_flight.get(run_id)
        if task is not None:
            task.cancel()


    def start(self) -> None:
        """Starts the background job-consuming and output-reporting loops; a no-op if already running."""
        if self._running:
            return
        self._running = True
        self._spawn(self._run_jobs(), name="provider-jobs")
        self._spawn(self._report_output(), name="provider-output")

    async def aclose(self, *, cancel_in_flight: bool = False) -> None:
        """Stops the runtime, optionally cancelling in-flight runs, and awaits its background tasks to finish."""
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
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task


    async def _run_jobs(self) -> None:
        while True:
            run = await self._jobs.get()
            task = self._spawn(self._execute(run), name=f"run:{run.run_id}")
            self._in_flight[run.run_id] = task
            task.add_done_callback(
                lambda _t, run_id=run.run_id: self._in_flight.pop(run_id, None)
            )

    async def _execute(self, run: DeliveredRun) -> None:
        """Streams the provider's events for `run` into the output queue, always enqueuing a terminal marker
        (triggering `finish_run`) even on cancellation or an unhandled exception."""
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
        while True:
            run_id, event = await self._output.get()
            try:
                if self.link is None:
                    continue
                if event is _END:
                    await self.link.finish_run(run_id)
                else:
                    await self.link.report_event(run_id, event)
            except Exception:
                logger.exception("run %s: reporting failed", run_id)


_END = object()
