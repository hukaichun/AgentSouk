from __future__ import annotations

import asyncio

import pytest
from ag_ui.core import RunAgentInput

from funduq_provider_sdk import DeliveredRun, FunduqLink


def _run_agent_input(**overrides) -> dict:
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

    def __init__(self, run_id: str, agent_name: str, run_input: dict, thread_id: str) -> None:
        self.run_id = run_id
        self.run_input = run_input
        self.thread_id = thread_id
        self.agent = type("AgentRef", (), {"name": agent_name})()


class QueuedLink(FunduqLink):

    def __init__(self, public_key: str, *, accept: bool = True, limit: int | None = 3) -> None:
        self._public_key = public_key
        self._accept = accept
        self._limit = limit
        self.outbound: asyncio.Queue = asyncio.Queue()
        self.cancelled: list[str] = []
        self.queried: list[tuple[str, int | None]] = []
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
        self.queried.append((thread_id, limit))
        return []


async def test_the_base_translates_funduqs_run_for_a_transport_that_never_sees_it():
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
    provider = QueuedLink("abc123", accept=False)

    assert await provider.deliver(_ClaimedRun("r-2", "a", _run_agent_input(), "t")) is False


async def test_an_invalid_input_is_a_refusal_not_a_transient_decline():
    from funduq_provider_sdk import Refusal

    provider = QueuedLink("abc123")

    answer = await provider.deliver(
        _ClaimedRun("r-bad", "a", {"not": "a RunAgentInput"}, "t")
    )

    assert isinstance(answer, Refusal)
    assert "RunAgentInput" in answer.reason
    assert provider.outbound.empty()


async def test_cancel_reaches_the_transport():
    provider = QueuedLink("abc123")
    provider.cancel("r-3")
    assert provider.cancelled == ["r-3"]


def test_a_transport_that_declares_nothing_is_not_constructible():

    class Forgetful(FunduqLink):
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


class LoopbackLink(FunduqLink):

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
        self.queried.append((thread_id, limit))
        return []


async def test_one_link_carries_a_run_down_and_its_results_back():
    from funduq_provider_sdk import AgentHandle, HandleProvider, ProviderIdentity, ProviderRuntime

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
    from funduq_provider_sdk import AgentHandle, HandleProvider, ProviderIdentity, ProviderRuntime

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
        await runtime.aclose()


async def test_the_runtime_hands_every_run_to_the_agent_as_it_arrives():
    """The runtime imposes no policy of its own: a run declared as an
    interjection (forwardedProps.addressedRunId) is delivered to the agent
    code exactly like any other — absorbing, deferring, or ignoring it is
    the author's decision, not the runtime's."""
    from funduq_provider_sdk import AgentHandle, HandleProvider, ProviderIdentity, ProviderRuntime

    seen: list = []

    async def agent(run_input: RunAgentInput):
        seen.append(run_input)
        yield {"type": "RUN_FINISHED", "threadId": "t", "runId": "r"}

    runtime = ProviderRuntime(
        ProviderIdentity.generate(), HandleProvider([AgentHandle("a", agent)]), max_queued_runs=2
    )
    runtime.start()
    try:
        declared = DeliveredRun(
            run_id="r2",
            agent_name="a",
            run_input=RunAgentInput(**{**_run_agent_input(), "forwardedProps": {"addressedRunId": "r1"}}),
        )
        assert await runtime.deliver(declared) is True, (
            "an interjection is a run like any other; the author judges it"
        )
        plain = DeliveredRun(
            run_id="r3", agent_name="a", run_input=RunAgentInput(**_run_agent_input())
        )
        assert await runtime.deliver(plain) is True
        async with asyncio.timeout(2):
            while len(seen) < 2:
                await asyncio.sleep(0)
        assert seen[0].forwarded_props == {"addressedRunId": "r1"}, (
            "the declaration reaches the author's code intact"
        )
    finally:
        await runtime.aclose()
