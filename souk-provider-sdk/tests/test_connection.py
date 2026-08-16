"""`SoukConnection` is only an abstraction if a second transport fits it.

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

from souk_provider_sdk import DeliveredRun, SoukConnection


class _ClaimedRun:
    """souk's shape, as this package must never import it."""

    def __init__(self, run_id: str, agent_name: str, run_input: dict, thread_id: str) -> None:
        self.run_id = run_id
        self.run_input = run_input
        self.thread_id = thread_id
        self.agent = type("AgentRef", (), {"name": agent_name})()


class QueuedProvider(SoukConnection):
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


async def test_the_base_translates_souks_run_for_a_transport_that_never_sees_it():
    provider = QueuedProvider("abc123")

    accepted = await provider.deliver(
        _ClaimedRun("r-1", "translator", {"messages": []}, "t-1")
    )

    assert accepted is True
    carried = provider.outbound.get_nowait()
    assert isinstance(carried, DeliveredRun)
    assert (carried.run_id, carried.agent_name, carried.thread_id) == ("r-1", "translator", "t-1")
    assert carried.run_input == {"messages": []}


async def test_declining_is_carried_through_unchanged():
    """The only way a provider says it is full, so the base must not swallow
    it or turn it into an exception."""
    provider = QueuedProvider("abc123", accept=False)

    assert await provider.deliver(_ClaimedRun("r-2", "a", {}, "t")) is False


async def test_cancel_reaches_the_transport():
    provider = QueuedProvider("abc123")
    provider.cancel("r-3")
    assert provider.cancelled == ["r-3"]


def test_a_transport_that_declares_nothing_is_not_constructible():
    """`max_concurrent_runs` is abstract on purpose: souk sizes its bucket
    from it, so a connection that omits it starves or overruns. Failing at
    construction beats failing inside souk's broker at registration."""

    class Forgetful(SoukConnection):
        @property
        def public_key(self) -> str:
            return "k"

        async def offer(self, run: DeliveredRun) -> bool:
            return True

        def cancel(self, run_id: str) -> None:
            pass

    with pytest.raises(TypeError, match="max_concurrent_runs"):
        Forgetful()
