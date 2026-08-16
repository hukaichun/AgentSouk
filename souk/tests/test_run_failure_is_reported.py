"""A run that fails has to *say so* on the stream, not only in the database.

Found against a real provider: `run_stream` raised, souk recorded the run as
`failed` with a failureReason, and the caller got HTTP 200 with an event
stream that closed in 0.1s having emitted nothing at all. A client cannot
tell that apart from an agent that had nothing to say, and every one of them
would have to poll the run's status to find out — for a fact souk already
knew and had already written down.

The rule these pin down: souk emits a terminal RUN_ERROR exactly when the
run ended `failed` and nobody else said so.
"""

from __future__ import annotations

import asyncio

import pytest

from souk import repo
from souk.config import CoreSettings
from souk.core import Souk

from tests.test_in_process_worker import _register, _until


@pytest.fixture
async def brisk(settings: CoreSettings):
    souk = Souk(settings.model_copy(update={"worker_poll_interval_seconds": 0.02}))
    try:
        yield souk
    finally:
        await souk.aclose()


async def test_a_provider_that_raises_reaches_the_caller_as_run_error(brisk):
    """The exact shape of the original defect: the provider blows up before
    emitting anything, so the stream's only content is souk's own account of
    what happened."""
    registration, public_key = await _register(brisk, "explodes")
    agent_id = registration.agents["explodes"]

    class Exploding:
        async def run_stream(self, agent_id: str, run_input: dict):
            raise KeyError("token")
            yield  # pragma: no cover — makes this an async generator

    await brisk.attach_provider(public_key, Exploding(), [agent_id.name])
    handle = await brisk.start_run(agent_id, {"messages": []})

    events = [e async for e in handle.events()]

    assert [e["type"] for e in events] == ["RUN_ERROR"]
    assert events[0]["code"] == "provider_stream_ended_without_finishing"

    await _until(lambda: handle.run_id not in brisk.active_runs())
    async with brisk.session() as session:
        stored = await repo.get_run(session, handle.run_id)
        # The same account either way: a caller that reconnects and reads the
        # run's stored events sees what the live stream showed.
        persisted = await repo.get_run_events(session, handle.run_id)
    assert stored.status == "failed"
    assert [e["type"] for e in persisted] == ["RUN_ERROR"]


async def test_a_provider_that_reports_its_own_failure_is_not_corrected(brisk):
    """souk speaks up only when nobody did. An agent that emits RUN_ERROR
    itself has already told the caller — and its own message is the useful
    one, so a second, vaguer event on top of it would be noise."""
    registration, public_key = await _register(brisk, "polite")
    agent_id = registration.agents["polite"]

    class ReportsItsOwn:
        async def run_stream(self, agent_id: str, run_input: dict):
            yield {"type": "RUN_STARTED", "threadId": run_input["threadId"], "runId": run_input["runId"]}
            yield {"type": "RUN_ERROR", "message": "upstream model refused the request"}

    await brisk.attach_provider(public_key, ReportsItsOwn(), [agent_id.name])
    handle = await brisk.start_run(agent_id, {"messages": []})

    events = [e async for e in handle.events()]

    assert [e["type"] for e in events] == ["RUN_STARTED", "RUN_ERROR"]
    assert events[-1]["message"] == "upstream model refused the request"

    await _until(lambda: handle.run_id not in brisk.active_runs())
    async with brisk.session() as session:
        assert (await repo.get_run(session, handle.run_id)).status == "failed"


async def test_a_cancelled_run_gets_no_run_error(brisk):
    """Not a failure, and AG-UI has no cancelled event to send anyway — so
    the stream simply ends. Inventing a RUN_ERROR here would tell the one
    party who already knows that their own request was a fault."""
    registration, public_key = await _register(brisk, "stoppable")
    agent_id = registration.agents["stoppable"]
    started = asyncio.Event()

    class WaitsForever:
        async def run_stream(self, agent_id: str, run_input: dict):
            yield {"type": "RUN_STARTED", "threadId": run_input["threadId"], "runId": run_input["runId"]}
            started.set()
            await asyncio.Event().wait()

    await brisk.attach_provider(public_key, WaitsForever(), [agent_id.name])
    handle = await brisk.start_run(agent_id, {"messages": []})

    stream = handle.events()
    assert (await stream.__anext__())["type"] == "RUN_STARTED"
    await started.wait()
    handle.cancel()

    assert [e["type"] async for e in stream] == []

    await _until(lambda: handle.run_id not in brisk.active_runs())
    async with brisk.session() as session:
        assert (await repo.get_run(session, handle.run_id)).status == "cancelled"
