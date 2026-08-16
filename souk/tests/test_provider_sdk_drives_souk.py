"""The SDK's worker, run against a real Souk.

The contract checks beside this one (test_provider_sdk_contract.py) enumerate
what a worker reads. This one just *uses* it, which covers everything the
enumeration cannot: that `claim_work`'s keyword arguments are still called
what the worker calls them, that the objects it hands back behave, that
`on_cancel` is invoked the way the worker expects, and that the whole loop
still turns.

Worth being explicit about why both exist. Using it catches almost
everything, and is cheap. What it cannot catch is a *silent* divergence —
rename souk's `NothingOwned` and the worker stops recognising it, falls
through to "unknown error, retry", and keeps looping; a test that only ran the
happy path would stay green while a provider went back to spinning forever.
That is the enumeration's job.

souk's in-process worker (`souk/worker.py`) is not this. A provider owns its
loop, and that souk ships one too is two implementations of one contract, not
duplication: everything a loop decides — capacity, pacing, whether to honour a
cancel — is a provider-side policy.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from souk_provider_sdk import AgentHandle, HandleProvider, ProviderIdentity, ProviderWorker

from sqlalchemy import delete

from souk.schema import agents


async def _until(predicate, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


async def _register(souk: Souk, identity: ProviderIdentity, *names: str):
    signature, timestamp = identity.sign_registration(list(names))
    return await souk.register_agents(
        identity.public_key, signature, timestamp, [{"name": n} for n in names]
    )


@pytest.fixture
async def workers():
    """Every worker a test starts, shut down when it ends.

    souk's `souk` fixture is session-scoped, so a worker left running does
    not go away with the test that made it — it keeps claiming into the next
    one. That is how `ProviderWorker.aclose` came to exist.
    """
    started: list[ProviderWorker] = []
    yield started
    for worker in started:
        await worker.aclose(cancel_in_flight=True)


def _worker(souk, identity, registration, names, **kwargs):
    return ProviderWorker(
        souk=souk,
        identity=identity,
        provider=HandleProvider([AgentHandle(n, kwargs.pop("agent")) for n in names]),
        agent_names=list(names),
        session_token=registration.session_token,
        poll_interval_seconds=0.02,
        long_poll_seconds=0.1,
        **kwargs,
    )


async def test_a_provider_sdk_worker_runs_a_real_run_end_to_end(souk, workers):
    """A `Souk` satisfies the SDK's connection protocol structurally — no
    adapter, no shim. If a keyword argument or a returned field is ever
    renamed on either side, this stops working."""
    identity = ProviderIdentity.generate()
    registration = await _register(souk, identity, "translator")

    seen: dict = {}

    async def agent(run_input):
        seen["input"] = run_input
        yield {"type": "RUN_STARTED", "threadId": run_input["threadId"], "runId": run_input["runId"]}
        yield {"type": "TEXT_MESSAGE_START", "messageId": "m1", "role": "assistant"}
        yield {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "bonjour"}
        yield {"type": "TEXT_MESSAGE_END", "messageId": "m1"}
        yield {"type": "RUN_FINISHED", "threadId": run_input["threadId"], "runId": run_input["runId"]}

    worker = _worker(souk, identity, registration, ["translator"], agent=agent)
    workers.append(worker)
    worker.start()
    try:
        handle = await souk.start_run(registration.agents["translator"], {"messages": []})
        events = [event async for event in handle.events()]
    finally:
        await worker.aclose(cancel_in_flight=True)

    assert [e["type"] for e in events] == [
        "RUN_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
    # Claiming *is* the hand-over: the worker left the call able to start.
    assert seen["input"]["runId"] == handle.run_id
    await _until(lambda: handle.run_id not in souk.active_runs())
    assert (await souk.get_run(handle.run_id)).status == "completed"


async def test_a_sdk_worker_honours_a_cancel_and_souk_records_what_it_saw(souk, workers):
    """`on_cancel` is how souk *asks*. That the worker complies is its own
    choice — this pins that souk's request reaches it at all."""
    identity = ProviderIdentity.generate()
    registration = await _register(souk, identity, "slow")
    started = asyncio.Event()

    async def agent(run_input):
        yield {"type": "RUN_STARTED", "threadId": run_input["threadId"], "runId": run_input["runId"]}
        started.set()
        await asyncio.sleep(30)

    worker = _worker(souk, identity, registration, ["slow"], agent=agent)
    workers.append(worker)
    worker.start()
    try:
        handle = await souk.start_run(registration.agents["slow"], {"messages": []})
        async with asyncio.timeout(5):
            await started.wait()
        handle.cancel()
        [_ async for _ in handle.events()]
    finally:
        await worker.aclose(cancel_in_flight=True)

    await _until(lambda: handle.run_id not in souk.active_runs())
    # No RUN_FINISHED and souk did ask, so the outcome is cancelled — decided
    # from what souk saw, never from what the worker asserted.
    assert (await souk.get_run(handle.run_id)).status == "cancelled"


async def test_a_worker_whose_agents_souk_forgets_says_so_and_keeps_going(souk, workers, caplog):
    """The path issue #37 is about, driven through the SDK rather than souk's
    own worker — which is the half that had no coverage at all.

    The database is emptied of this provider rather than the agent being
    deleted, because that is the trigger the issue actually reported: a
    restore, or souk repointed at a fresh database, while a provider's
    connection stays open.
    """
    identity = ProviderIdentity.generate()
    registration = await _register(souk, identity, "ephemeral")

    async def agent(run_input):
        yield {"type": "RUN_FINISHED", "threadId": run_input["threadId"], "runId": run_input["runId"]}

    worker = _worker(souk, identity, registration, ["ephemeral"], agent=agent)
    with caplog.at_level(logging.ERROR, logger="souk_provider_sdk.worker"):
        async with souk.session() as write:
            await write.execute(delete(agents).where(agents.c.provider_key == identity.public_key))
            await write.commit()
        workers.append(worker)
        worker.start()
        try:
            await _until(
                lambda: any("registered none of" in r.getMessage() for r in caplog.records)
            )
            # Loud, and still running: the names have not changed, so
            # registering them again is the whole repair.
            assert worker._loop_task is not None and not worker._loop_task.done()
        finally:
            await worker.aclose(cancel_in_flight=True)
