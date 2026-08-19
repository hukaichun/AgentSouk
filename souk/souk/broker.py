from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from souk.live_roster import LiveRoster
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
    """Per-provider counters of protocol violations observed while dispatching:
    declining an offer after claiming to have room (misdeclared), taking a run
    and never ending it (abandoned), not answering an offer within the delivery
    timeout (unanswered), and acking after souk gave up waiting (answered_late)."""

    in_flight: int
    declared: int | None
    misdeclared: int
    abandoned: int
    unanswered: int
    answered_late: int


@dataclass
class _Capacity:
    """Tracks one provider's in-flight run count against its declared limit.
    `declared=None` means the provider accepts unlimited concurrent runs."""

    declared: int | None
    in_flight: int = 0

    @property
    def has_room(self) -> bool:
        return self.declared is None or self.in_flight < self.declared


class Refusal(Protocol):
    """A permanent decline of an offer, read duck-typed off `deliver`'s return: any object with a `reason` string.

    The attribute name is the contract with `souk_provider_sdk.provider.Refusal`,
    which builds these on the provider side — neither package imports the other.
    """

    reason: str


class ConnectedProvider(Protocol):

    public_key: str
    max_concurrent_runs: int | None

    async def deliver(self, run: ClaimedRun) -> bool | Refusal:
        ...

    def cancel(self, run_id: str) -> None:
        ...


@dataclass
class Run:
    """A broker-side run's mutable dispatch state: its position in the pending
    queue, which provider (if any) has claimed it, and the in/out queues that
    feed its `_pipeline` task. Distinct from `ClaimedRun`, the read-only value
    handed to a provider's `deliver`."""

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
    """Yields items placed on `run.out_queue` until the END_OF_STREAM sentinel
    (put there when the run's pipeline finishes), then returns."""
    while True:
        item = await run.out_queue.get()
        if item is END_OF_STREAM:
            return
        yield item


async def _no_events() -> AsyncIterator[Any]:
    """An empty async iterator, returned by `subscribe` for an unknown run_id."""
    return
    yield  # pragma: no cover - what makes this an async generator


HandlerMap = dict[type, Callable[[Run, Any], Awaitable[None]]]

async def _pipeline(run: Run, handlers: HandlerMap, owner: "RunBroker") -> None:
    """Drains `run.in_queue`, dispatching each command to its registered handler
    in order. Stops after a `FinishStream` or `Fail`, or after a `RequestCancel`
    for a run nobody has claimed; a `RequestCancel` for a claimed run is handled
    but the pipeline keeps running, waiting for the eventual terminal command.
    On exit it signals END_OF_STREAM on `run.out_queue` and calls `owner.forget`."""
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
    """Matches queued runs to connected providers, one agent's pending runs at
    a time, respecting each provider's declared concurrency, and tracks
    per-provider quality-of-service counters (`ProviderQuality`). Must be
    `start()`-ed before `enqueue_run` will accept work; `start`/`stop` may be
    called across separate event loops as long as they don't overlap."""

    def __init__(
        self,
        spawn=None,
        *,
        sweep_interval_seconds: float = 1.0,
        unserved_timeout_seconds: float = 45.0,
        deliver_timeout_seconds: float = 5.0,
    ) -> None:
        self._spawn = spawn or self._spawn_unsupervised
        self._runs: dict[str, Run] = {}
        self._pending_by_agent: dict[AgentRef, deque[str]] = defaultdict(deque)
        self._live = LiveRoster(("misdeclared", "abandoned", "unanswered", "answered_late"))
        self._unserved_since: dict[AgentRef, datetime] = {}
        self._capacity: dict[str, _Capacity] = {}
        self._handlers: dict[str, HandlerMap] = {}
        self._pipeline_tasks: set[asyncio.Task] = set()
        self.sweep_interval_seconds = sweep_interval_seconds
        self.unserved_timeout_seconds = unserved_timeout_seconds
        self.deliver_timeout_seconds = deliver_timeout_seconds
        self._loop_task: asyncio.Task | None = None
        self._work_to_do = asyncio.Event()
        self._forget_listeners: list[Callable[[str], None]] = []

    def add_forget_listener(self, listener: Callable[[str], None]) -> None:
        self._forget_listeners.append(listener)


    def start(self) -> None:
        """Starts the sweep loop if it isn't already running. Recreates the
        internal wakeup event each time, so a broker built outside a running
        event loop can still be started and stopped in successive loops."""
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
        """Repeatedly gives up on queued runs whose agent has gone unserved for
        too long and offers pending runs to their providers, sleeping until new
        work arrives (`enqueue_run`, `register_provider`) or the unserved
        timeout elapses. Swallows and logs any exception other than
        cancellation so one bad sweep doesn't stop future ones."""
        while True:
            try:
                self.expire_queued(self.unserved_timeout_seconds)
                self._work_to_do.clear()
                placed = False
                for agent in list(self._pending_by_agent):
                    if await self._offer_pending(agent):
                        placed = True
                if placed:
                    await asyncio.sleep(0)
                    continue
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(self.unserved_timeout_seconds):
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
        """Queues a new run for `agent` and wakes the sweep loop to offer it.
        Raises `RuntimeError` if the broker hasn't been `start()`-ed, since
        nothing would ever pick the run up."""
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
        """Registers (or replaces) the provider serving each given agent and
        wakes the sweep loop. A provider re-registering under a key it already
        has capacity tracking for keeps its existing in-flight count rather
        than resetting it."""
        self._live.attach(mapping)
        for agent in mapping:
            self._unserved_since.pop(agent, None)
        for provider in mapping.values():
            self._capacity.setdefault(
                provider.public_key, _Capacity(declared=provider.max_concurrent_runs)
            )
        self._work_to_do.set()

    def serving(self, agent: AgentRef) -> ConnectedProvider | None:
        return self._live.serving(agent)

    def agents_served_by(self, public_key: str) -> list[AgentRef]:
        return self._live.served_by(public_key)

    def unregister_provider(self, agents: list[AgentRef]) -> None:
        now = datetime.now(timezone.utc)
        self._live.withdraw(agents)
        for agent in agents:
            if self._live.serving(agent) is None:
                self._unserved_since[agent] = now

    async def _offer_pending(self, agent: AgentRef) -> bool:
        """Offers `agent`'s queued runs, in order, to its provider until the
        queue is empty, the provider has no room, or an offer isn't accepted.
        Skips (and drops) runs that were cancelled while still queued. Returns
        whether at least one run was placed."""
        placed = False
        while True:
            provider = self._live.serving(agent)
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
            outcome = await self._offer(run, provider)
            if outcome == "refused":
                queue.popleft()
                continue
            if not outcome:
                return placed
            queue.popleft()
            placed = True

    async def _offer(self, run: Run, provider: ConnectedProvider) -> bool | str:
        """Delivers `run` to `provider` within `deliver_timeout_seconds` and, if
        accepted, claims it and starts its pipeline task. A timeout, an
        exception, or a declined offer all count against the provider's
        quality counters (unanswered or misdeclared) and return False without
        claiming the run. A permanent refusal (a result carrying a `reason`)
        fails the run with the provider's reason recorded verbatim and
        returns "refused" so the caller drops it from the queue instead of
        re-offering forever."""
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
            self._live.note(provider.public_key, "unanswered")
            logger.warning(
                "provider %s did not answer an offer of run %s within %ss (%d so far)",
                provider.public_key[:16],
                run.run_id,
                self.deliver_timeout_seconds,
                self._live.count(provider.public_key, "unanswered"),
            )
            return False
        except Exception:
            self._live.note(provider.public_key, "unanswered")
            logger.exception("run %s: delivering to its provider failed", run.run_id)
            return False
        reason = getattr(accepted, "reason", None)
        if isinstance(reason, str):
            logger.warning(
                "provider %s permanently refused run %s: %s",
                provider.public_key[:16],
                run.run_id,
                reason,
            )
            self._spawn(self._one_shot(run, Fail(reason)), name=f"refused:{run.run_id}")
            return "refused"
        if not accepted:
            if capacity is not None and capacity.has_room:
                self._live.note(provider.public_key, "misdeclared")
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
        """Enqueues `command` on a claimed run's pipeline. Returns False if
        `run_id` is unknown. Pushing a `Fail` for a run its provider had
        claimed is recorded as an abandoned run in that provider's quality
        counters."""
        run = self._runs.get(run_id)
        if run is None:
            return False
        if isinstance(command, Fail) and run.claimed_by is not None:
            self._live.note(run.claimed_by, "abandoned")
            logger.warning(
                "provider %s abandoned run %s (%d so far): took it and never ended it",
                run.claimed_by[:16],
                run_id,
                self._live.count(run.claimed_by, "abandoned"),
            )
        run.in_queue.put_nowait(command)
        return True

    def subscribe(self, run_id: str) -> AsyncIterator[Any]:
        """Returns an async iterator of whatever is pushed to `run_id`'s
        out_queue, ending when its pipeline finishes; an empty iterator if
        `run_id` is unknown."""
        run = self._runs.get(run_id)
        return _drain_run(run) if run is not None else _no_events()

    def request_cancel(self, run_id: str) -> bool:
        """Marks the run cancel-requested immediately, then either forwards a
        `RequestCancel` into its pipeline (if a provider has claimed it) or, if
        it is still only queued, runs the `RequestCancel` handler once directly
        so it still gets recorded and the run ends without ever being offered.
        Returns False if `run_id` is unknown."""
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
        """Gives up on queued (unclaimed) runs whose agent has had no serving
        provider for longer than `timeout_seconds`, failing each with
        `Fail("no_provider_took_it")`, and returns their run_ids. A run whose
        agent *is* served stays queued indefinitely — a declining-but-attached
        provider is a full stall, not a lost one. The clock is the later of the
        run's own enqueue and the moment the agent last lost its provider, so
        every run gets the full grace period even if its agent was already
        unserved when it arrived."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
        expired: list[str] = []
        for agent, queue in list(self._pending_by_agent.items()):
            if self._live.serving(agent) is not None:
                continue
            unserved_since = self._unserved_since.get(agent)
            for run_id in list(queue):
                run = self._runs.get(run_id)
                if run is None:
                    continue
                reference = (
                    max(run.queued_at, unserved_since) if unserved_since else run.queued_at
                )
                if reference > cutoff:
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
        """Runs a single command's handler directly (bypassing `_pipeline`) for
        a run that was never claimed, then ends the run the same way the
        pipeline would: signals END_OF_STREAM and forgets it. Used for expiring
        queued runs and for cancelling a run before any provider claimed it."""
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
        """Lets a provider claim a run after souk already gave up waiting for
        its answer (e.g. an unanswered offer timed out), as long as the run
        hasn't already been claimed and `claimed_by` matches the provider
        currently registered for that agent. On success, removes the run from
        the pending queue if it's still sitting there, records an
        `answered_late` quality event, and starts its pipeline. Returns False
        (without changing anything) if the run is already claimed or
        `claimed_by` doesn't match."""
        run = self._runs.get(run_id)
        if run is None or run.claimed_by is not None:
            return False
        provider = self._live.serving(run.agent)
        if provider is None or provider.public_key != claimed_by:
            return False

        queue = self._pending_by_agent.get(run.agent)
        if queue is not None and run_id in queue:
            queue.remove(run_id)

        capacity = self._capacity.get(claimed_by)
        if capacity is not None:
            capacity.in_flight += 1
        self._live.note(claimed_by, "answered_late")
        logger.warning(
            "provider %s answered late for run %s (%d so far): already producing for "
            "a run souk had put back in the queue",
            claimed_by[:16],
            run_id,
            self._live.count(claimed_by, "answered_late"),
        )
        run.claimed_by = claimed_by
        run.cancel_notify = provider.cancel
        handlers = self._handlers.get(run_id)
        if handlers is not None:
            self._spawn(_pipeline(run, handlers, self), name=f"pipeline:{run_id}")
        run.in_queue.put_nowait(Claim())
        return True

    def quality(self) -> dict[str, ProviderQuality]:
        counters = self._live.counters()
        return {
            key: ProviderQuality(
                in_flight=c.in_flight,
                declared=c.declared,
                **{
                    name: counters.get(key, {}).get(name, 0)
                    for name in ("misdeclared", "abandoned", "unanswered", "answered_late")
                },
            )
            for key, c in self._capacity.items()
        }

    def forget(self, run_id: str) -> None:
        """Drops a run's tracked state and handlers, frees the claimed
        provider's in-flight capacity (if any, waking the sweep loop so its
        freed place can be offered again), and notifies forget listeners.
        Safe to call for a run that isn't tracked."""
        run = self._runs.pop(run_id, None)
        self._handlers.pop(run_id, None)
        if run is not None and run.claimed_by is not None:
            capacity = self._capacity.get(run.claimed_by)
            if capacity is not None and capacity.in_flight > 0:
                capacity.in_flight -= 1
                self._work_to_do.set()
        for listener in self._forget_listeners:
            listener(run_id)
