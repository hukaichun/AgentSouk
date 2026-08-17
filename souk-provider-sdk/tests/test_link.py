"""`SoukLink` is only an abstraction if a second transport fits it.

One implementation is not an abstraction, it is a class with extra steps. So
this file writes a second one — a queue with an ack, which is the shape a
socket has and a function call does not — and checks the base carries it.

The point is `deliver`: souk's field names are read there, once, for every
transport. The queue implementation below never mentions `run.agent.name`,
and neither would a real socket binding.
"""

from __future__ import annotations

import asyncio

import pytest
from ag_ui.core import RunAgentInput

from souk_provider_sdk import DeliveredRun, SoukLink


def _run_agent_input(**overrides) -> dict:
    """A minimal but complete `RunAgentInput` dict, souk's own spelling
    (camelCase) — this is what `run.run_input` looks like on the wire between
    souk and this package's `deliver`, which validates against it."""
    base = {
        "threadId": "t-1",
        "runId": "r-1",
        "state": None,
        "messages": [],
        "tools": [],
        "context": [],
        "forwardedProps": None,
    }
    base.update(overrides)
    return base


class _ClaimedRun:
    """souk's shape, as this package must never import it."""

    def __init__(self, run_id: str, agent_name: str, run_input: dict, thread_id: str) -> None:
        self.run_id = run_id
        self.run_input = run_input
        self.thread_id = thread_id
        self.agent = type("AgentRef", (), {"name": agent_name})()


class QueuedLink(SoukLink):
    """The far end is a queue and an ack — a socket without the socket.

    Deliberately holds no runtime: this is the shape a *gateway*-side
    connection has, where the thing that executes runs is across a wire.
    """

    def __init__(self, public_key: str, *, accept: bool = True, limit: int | None = 3) -> None:
        self._public_key = public_key
        self._accept = accept
        self._limit = limit
        self.outbound: asyncio.Queue = asyncio.Queue()
        self.cancelled: list[str] = []
        self.queried: list[tuple[str, int | None]] = []
        # The upward half: a real socket link would encode and write these.
        self.reported: list[tuple[str, object]] = []
        self.finished: list[str] = []

    @property
    def public_key(self) -> str:
        return self._public_key

    @property
    def max_concurrent_runs(self) -> int | None:
        return self._limit

    async def offer(self, run: DeliveredRun) -> bool:
        self.outbound.put_nowait(run)
        return self._accept

    def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)

    async def report_event(self, run_id: str, event) -> None:
        self.reported.append((run_id, event))

    async def finish_run(self, run_id: str) -> None:
        self.finished.append(run_id)

    async def thread_messages(self, thread_id: str, *, limit: int | None = None):
        # A socket link would send `limit` and let the far side slice; this
        # stand-in just records what it was asked for.
        self.queried.append((thread_id, limit))
        return []


async def test_the_base_translates_souks_run_for_a_transport_that_never_sees_it():
    provider = QueuedLink("abc123")

    accepted = await provider.deliver(
        _ClaimedRun("r-1", "translator", _run_agent_input(), "t-1")
    )

    assert accepted is True
    carried = provider.outbound.get_nowait()
    assert isinstance(carried, DeliveredRun)
    assert (carried.run_id, carried.agent_name, carried.thread_id) == ("r-1", "translator", "t-1")
    assert isinstance(carried.run_input, RunAgentInput)
    assert (carried.run_input.thread_id, carried.run_input.run_id) == ("t-1", "r-1")


async def test_declining_is_carried_through_unchanged():
    """The only way a provider says it is full, so the base must not swallow
    it or turn it into an exception."""
    provider = QueuedLink("abc123", accept=False)

    assert await provider.deliver(_ClaimedRun("r-2", "a", _run_agent_input(), "t")) is False


async def test_cancel_reaches_the_transport():
    provider = QueuedLink("abc123")
    provider.cancel("r-3")
    assert provider.cancelled == ["r-3"]


def test_a_transport_that_declares_nothing_is_not_constructible():
    """`max_concurrent_runs` is abstract on purpose: souk sizes its bucket
    from it, so a connection that omits it starves or overruns. Failing at
    construction beats failing inside souk's broker at registration."""

    class Forgetful(SoukLink):
        @property
        def public_key(self) -> str:
            return "k"

        async def offer(self, run: DeliveredRun) -> bool:
            return True

        def cancel(self, run_id: str) -> None:
            pass

        async def report_event(self, run_id: str, event) -> None:
            pass

        async def finish_run(self, run_id: str) -> None:
            pass

        async def thread_messages(self, thread_id: str, *, limit: int | None = None):
            return []

    with pytest.raises(TypeError, match="max_concurrent_runs"):
        Forgetful()



class LoopbackLink(SoukLink):
    """A link with a real runtime under it, and a list where a socket would be.

    What `InProcessLink` is, minus the souk: downward it forwards to the
    runtime, upward it records. One object, both directions — which is the
    claim `SoukLink` exists to make.
    """

    def __init__(self, runtime) -> None:
        self._runtime = runtime
        runtime.link = self
        self.reported: list[tuple[str, object]] = []
        self.finished: list[str] = []
        self.queried: list[tuple[str, int | None]] = []

    @property
    def public_key(self) -> str:
        return self._runtime.public_key

    @property
    def max_concurrent_runs(self) -> int | None:
        return self._runtime.max_concurrent_runs

    async def offer(self, run: DeliveredRun) -> bool:
        return await self._runtime.deliver(run)

    def cancel(self, run_id: str) -> None:
        self._runtime.cancel(run_id)

    async def report_event(self, run_id: str, event) -> None:
        self.reported.append((run_id, event))

    async def finish_run(self, run_id: str) -> None:
        self.finished.append(run_id)

    async def thread_messages(self, thread_id: str, *, limit: int | None = None):
        # A socket link would send `limit` and let the far side slice; this
        # stand-in just records what it was asked for.
        self.queried.append((thread_id, limit))
        return []


async def test_one_link_carries_a_run_down_and_its_results_back():
    """The reason the two halves are one object.

    A run goes down through `deliver` → `offer` → the runtime, and its events
    come back up through `report_event` / `finish_run` — on the same link,
    which over a wire is the same socket. They were split before, the
    downward half a base class and the upward half two loose callables on the
    runtime, and the split was accidental: the in-process implementation
    always did both.
    """
    from souk_provider_sdk import AgentHandle, HandleProvider, ProviderIdentity, ProviderRuntime

    async def agent(run_input: RunAgentInput):
        ids = {"threadId": run_input.thread_id, "runId": run_input.run_id}
        yield {"type": "RUN_STARTED", **ids}
        yield {"type": "RUN_FINISHED", **ids}

    runtime = ProviderRuntime(
        ProviderIdentity.generate(), HandleProvider([AgentHandle("a", agent)])
    )
    link = LoopbackLink(runtime)
    runtime.start()

    try:
        # souk's shape in — the base class converts it, this transport never
        # sees `run.agent.name`.
        # A realistic run_input: souk's `build_run_agent_input` puts the ids
        # it minted in there, which is what an agent echoes back on its events.
        assert await link.deliver(_ClaimedRun("r-1", "a", _run_agent_input(), "t-1")) is True

        async with asyncio.timeout(5):
            while not link.finished:
                await asyncio.sleep(0.005)
    finally:
        await runtime.aclose()

    assert [e["type"] for _, e in link.reported] == ["RUN_STARTED", "RUN_FINISHED"]
    assert {run_id for run_id, _ in link.reported} == {"r-1"}
    assert link.finished == ["r-1"]


async def test_a_runtime_with_no_link_drops_its_output_rather_than_raising():
    """`link` is set by the link's constructor, so there is a window before
    it. Output produced then is dropped on purpose — reporting belongs to the
    caller, and one missing link must not kill the single consumer every
    run's ordering depends on.
    """
    from souk_provider_sdk import AgentHandle, HandleProvider, ProviderIdentity, ProviderRuntime

    async def agent(run_input: RunAgentInput):
        yield {"type": "RUN_FINISHED", "threadId": "t", "runId": "r"}

    runtime = ProviderRuntime(
        ProviderIdentity.generate(), HandleProvider([AgentHandle("a", agent)])
    )
    runtime.start()
    try:
        assert runtime.link is None
        assert await runtime.deliver(
            DeliveredRun(run_id="r", agent_name="a", run_input=RunAgentInput(**_run_agent_input()))
        )
        await asyncio.sleep(0.05)
    finally:
        await runtime.aclose()  # the loop survived; nothing raised
