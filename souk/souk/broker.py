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

END_OF_STREAM = object()


@dataclass
class Claim:
    pass


@dataclass
class RelayEvent:

    event: Any


@dataclass
class FinishStream:
    pass


@dataclass
class RequestCancel:
    pass


@dataclass
class Fail:

    reason: str


Command = Claim | RelayEvent | FinishStream | RequestCancel | Fail


@dataclass(frozen=True)
class ProviderQuality:

    in_flight: int
    declared: int | None
    misdeclared: int
    abandoned: int
    unanswered: int
    answered_late: int


@dataclass
class _Capacity:

    declared: int | None
    in_flight: int = 0
    misdeclared: int = 0
    abandoned: int = 0
    unanswered: int = 0
    answered_late: int = 0

    @property
    def has_room(self) -> bool:
        return self.declared is None or self.in_flight < self.declared


class ConnectedProvider(Protocol):

    public_key: str
    max_concurrent_runs: int | None

    async def deliver(self, run: ClaimedRun) -> bool:
        ...

    def cancel(self, run_id: str) -> None:
        ...


@dataclass
class Run:

    run_id: str
    agent: AgentRef
    thread_id: str
    input_json: dict[str, Any]
    protocol: str
    queued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    seq: int = 0
    round_starting_seq: int = 0
    pause_payload: dict[str, Any] | None = None
    claimed_by: str | None = None
    cancel_notify: Callable[[str], None] | None = None
    cancel_requested: bool = False
    saw_run_finished: bool = False
    saw_run_error: bool = False
    in_queue: asyncio.Queue[Command] = field(default_factory=asyncio.Queue)
    out_queue: asyncio.Queue[Any] = field(default_factory=asyncio.Queue)


@dataclass(frozen=True)
class RunSnapshot:

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
    run.cancel_requested = True
    run.in_queue.put_nowait(RequestCancel())


async def _drain_run(run: Run) -> AsyncIterator[Any]:
    while True:
        item = await run.out_queue.get()
        if item is END_OF_STREAM:
            return
        yield item


async def _no_events() -> AsyncIterator[Any]:
    return
    yield  # pragma: no cover - what makes this an async generator


HandlerMap = dict[type, Callable[[Run, Any], Awaitable[None]]]

async def _pipeline(run: Run, handlers: HandlerMap, owner: "RunBroker") -> None:
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
        self._spawn = spawn or self._spawn_unsupervised
        self._runs: dict[str, Run] = {}
        self._pending_by_agent: dict[AgentRef, deque[str]] = defaultdict(deque)
        self._providers: dict[AgentRef, ConnectedProvider] = {}
        self._capacity: dict[str, _Capacity] = {}
        self._handlers: dict[str, HandlerMap] = {}
        self._pipeline_tasks: set[asyncio.Task] = set()
        self.sweep_interval_seconds = sweep_interval_seconds
        self.queued_timeout_seconds = queued_timeout_seconds
        self.deliver_timeout_seconds = deliver_timeout_seconds
        self._loop_task: asyncio.Task | None = None
        self._work_to_do = asyncio.Event()
        self._forget_listeners: list[Callable[[str], None]] = []

    def add_forget_listener(self, listener: Callable[[str], None]) -> None:
        self._forget_listeners.append(listener)


    def start(self) -> None:
        if not self.is_running:
            self._work_to_do = asyncio.Event()
            self._loop_task = self._spawn(self.run_forever(), name="broker-sweep")

    @property
    def is_running(self) -> bool:
        return self._loop_task is not None and not self._loop_task.done()

    def stop(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None

    async def run_forever(self) -> None:
        while True:
            try:
                self.expire_queued(self.queued_timeout_seconds)
                self._work_to_do.clear()
                placed = False
                for agent in list(self._pending_by_agent):
                    if await self._offer_pending(agent):
                        placed = True
                if placed:
                    await asyncio.sleep(0)
                    continue
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(self.queued_timeout_seconds):
                        await self._work_to_do.wait()
            except asyncio.CancelledError:
                raise
            except Exception:
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
        if not self.is_running:
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


    def register_provider(self, mapping: dict[AgentRef, ConnectedProvider]) -> None:
        self._providers.update(mapping)
        for provider in mapping.values():
            self._capacity.setdefault(
                provider.public_key, _Capacity(declared=provider.max_concurrent_runs)
            )
        self._work_to_do.set()

    def serving(self, agent: AgentRef) -> ConnectedProvider | None:
        return self._providers.get(agent)

    def agents_served_by(self, public_key: str) -> list[AgentRef]:
        return [a for a, p in self._providers.items() if p.public_key == public_key]

    def unregister_provider(self, agents: list[AgentRef]) -> None:
        for agent in agents:
            self._providers.pop(agent, None)

    async def _offer_pending(self, agent: AgentRef) -> bool:
        placed = False
        while True:
            provider = self._providers.get(agent)
            queue = self._pending_by_agent.get(agent)
            if provider is None or not queue:
                return placed
            run_id = queue[0]
            run = self._runs.get(run_id)
            if run is None or run.cancel_requested:
                queue.popleft()
                continue
            capacity = self._capacity.get(provider.public_key)
            if capacity is not None and not capacity.has_room:
                return placed
            if not await self._offer(run, provider):
                return placed
            queue.popleft()
            placed = True

    async def _offer(self, run: Run, provider: ConnectedProvider) -> bool:
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
        run.claimed_by = provider.public_key
        run.cancel_notify = provider.cancel
        if capacity is not None:
            capacity.in_flight += 1
        handlers = self._handlers.get(run.run_id)
        if handlers is not None:
            self._spawn(_pipeline(run, handlers, self), name=f"pipeline:{run.run_id}")
        run.in_queue.put_nowait(Claim())
        return True

    def _spawn_unsupervised(self, coro, *, name: str | None = None) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._pipeline_tasks.add(task)
        task.add_done_callback(self._pipeline_tasks.discard)
        return task

    def get(self, run_id: str) -> RunSnapshot | None:
        run = self._runs.get(run_id)
        return _snapshot(run) if run is not None else None

    def push(self, run_id: str, command: Command) -> bool:
        run = self._runs.get(run_id)
        if run is None:
            return False
        if isinstance(command, Fail) and run.claimed_by is not None:
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
        run = self._runs.get(run_id)
        return _drain_run(run) if run is not None else _no_events()

    def request_cancel(self, run_id: str) -> bool:
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
        await self._one_shot(run, RequestCancel())

    async def _one_shot(self, run: Run, command: Command) -> None:
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
        return list(self._runs)

    def accept_late_ack(self, run_id: str, claimed_by: str) -> bool:
        run = self._runs.get(run_id)
        if run is None or run.claimed_by is not None:
            return False
        provider = self._providers.get(run.agent)
        if provider is None or provider.public_key != claimed_by:
            return False

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
        run = self._runs.pop(run_id, None)
        self._handlers.pop(run_id, None)
        if run is not None and run.claimed_by is not None:
            capacity = self._capacity.get(run.claimed_by)
            if capacity is not None and capacity.in_flight > 0:
                capacity.in_flight -= 1
                self._work_to_do.set()
        for listener in self._forget_listeners:
            listener(run_id)
