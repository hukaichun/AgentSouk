from __future__ import annotations

import asyncio

import pytest

from souk import repo
from souk.core import Souk
from souk.models import AgentRef


class EchoProvider:

    async def run_stream(self, agent_id: str, run_input):
        text = run_input.messages[-1].content if run_input.messages else ""
        yield {"type": "RUN_STARTED", "threadId": run_input.thread_id, "runId": run_input.run_id}
        yield {"type": "TEXT_MESSAGE_START", "messageId": "m1", "role": "assistant"}
        yield {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": f"echo: {text}"}
        yield {"type": "TEXT_MESSAGE_END", "messageId": "m1"}
        yield {"type": "RUN_FINISHED", "threadId": run_input.thread_id, "runId": run_input.run_id}


class NeverFinishesProvider:

    async def run_stream(self, agent_id: str, run_input):
        yield {"type": "RUN_STARTED", "threadId": run_input.thread_id, "runId": run_input.run_id}
        await asyncio.Event().wait()


async def _register(souk, name: str, identity) -> str:
    async with souk.session() as session:
        registered = await repo.register_agents(session, identity.public_key, [{"name": name}])
    return registered[name]


async def _register_with_token(souk, name: str, identity):
    body = identity.register_body([{"name": name}])
    return await souk.register_agents(
        body["public_key"], body["signature"], body["timestamp"], body["agents"]
    )


async def _until(predicate, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


async def test_attach_start_and_read_back(souk, new_identity, attach):
    identity = new_identity()
    agent_id = await _register(souk, "echo", identity)
    await attach(identity, EchoProvider(), [agent_id.name])

    handle = await souk.start_run(agent_id, {"messages": [{"role": "user", "content": "hi"}]})

    assert handle.run_id.startswith("run_")
    assert handle.thread_id.startswith("thread_")

    events = [event async for event in handle.events()]
    assert [e["type"] for e in events][0] == "RUN_STARTED"
    assert any(e.get("delta") == "echo: hi" for e in events)

    await _until(lambda: handle.run_id not in souk.active_runs())

    run = await souk.get_run(handle.run_id)
    assert run.status == "completed"

    messages = await souk.get_thread_messages(handle.thread_id)
    assert messages[-1]["content"] == "echo: hi"

    assert len(await souk.get_run_events(handle.run_id)) == len(events)


async def test_roster_and_agent_lookup(souk, new_identity, attach):
    identity = new_identity()
    agent_id = await _register(souk, "echo", identity)

    roster = await souk.list_agents()
    assert [a.name for a in roster] == ["echo"]
    assert roster[0].online is False

    await attach(identity, EchoProvider(), ["echo"])
    assert (await souk.list_agents())[0].online is True

    assert (await souk.get_agent(agent_id)).name == "echo"
    assert await souk.get_agent(AgentRef(provider_key=agent_id.provider_key, name="nope")) is None


async def test_cancel_a_running_agent(souk, new_identity, attach):
    identity = new_identity()
    agent_id = await _register(souk, "slow", identity)
    await attach(identity, NeverFinishesProvider(), [agent_id.name])

    handle = await souk.start_run(agent_id, {"messages": []})
    stream = handle.events()
    assert (await anext(stream))["type"] == "RUN_STARTED"

    assert souk.cancel_run(handle.run_id) is True
    assert [e async for e in stream] == []
    await _until(lambda: handle.run_id not in souk.active_runs())

    run = await souk.get_run(handle.run_id)
    assert run.status == "cancelled"

    assert souk.cancel_run(handle.run_id) is False


class StubbornProvider:

    def __init__(self, identity) -> None:
        self.public_key = identity.public_key
        self.sign_connect = identity.sign_connect
        self.max_concurrent_runs = None
        self.taken: list[str] = []
        self.asked_to_stop: list[str] = []

    async def deliver(self, run) -> bool:
        self.taken.append(run.run_id)
        return True

    def cancel(self, run_id: str) -> None:
        self.asked_to_stop.append(run_id)


async def test_a_worker_that_ignores_the_cancel_still_completes(souk, new_identity):
    identity = new_identity()
    registration = await _register_with_token(souk, "stubborn", identity)
    agent_id = registration.agents["stubborn"]
    provider = StubbornProvider(identity)
    await souk.attach_provider(provider, ["stubborn"])

    handle = await souk.start_run(agent_id, {"messages": []})
    await _until(lambda: provider.taken == [handle.run_id])

    souk.cancel_run(handle.run_id)
    assert souk.broker.get(handle.run_id).cancel_requested is True
    await _until(lambda: provider.asked_to_stop == [handle.run_id])
    souk.report_event(
        handle.run_id,
        {"type": "RUN_FINISHED", "threadId": handle.thread_id, "runId": handle.run_id},
        claimed_by=identity.public_key,
    )
    souk.finish_run(handle.run_id, claimed_by=identity.public_key)

    events = [e async for e in handle.events()]
    await _until(lambda: handle.run_id not in souk.active_runs())

    assert events[-1]["type"] == "RUN_FINISHED"
    assert (await souk.get_run(handle.run_id)).status == "completed"


async def test_a_cancel_before_any_provider_takes_the_run(souk, new_identity):
    agent_id = await _register(souk, "never-claimed", new_identity())
    handle = await souk.start_run(agent_id, {"messages": []})

    assert souk.cancel_run(handle.run_id) is True
    await _until(lambda: handle.run_id not in souk.active_runs())

    assert (await souk.get_run(handle.run_id)).status == "cancelled"


async def test_thread_lineage(souk, new_identity):
    identity = new_identity()
    parent_agent = await _register(souk, "parent", identity)
    child_agent = await _register(souk, "child", identity)

    root = await souk.create_thread(parent_agent)
    async with souk.session() as session:
        child = await repo.create_thread(session, child_agent, parent_thread_id=root)
        grandchild = await repo.create_thread(session, child_agent, parent_thread_id=child)
        await session.commit()

    tree = await souk.get_thread_tree(root)
    assert tree["thread_id"] == root
    assert tree["children"][0]["thread_id"] == child
    assert tree["children"][0]["children"][0]["thread_id"] == grandchild

    assert await souk.get_thread_tree("thread_nope") is None


async def test_start_run_reuses_an_existing_thread(souk, new_identity, attach):
    identity = new_identity()
    agent_id = await _register(souk, "echo", identity)
    await attach(identity, EchoProvider(), [agent_id.name])

    thread_id = await souk.create_thread(agent_id)
    first = await souk.start_run(agent_id, {"messages": []}, thread_id=thread_id)
    assert first.thread_id == thread_id
    [_ async for _ in first.events()]

    thread = await souk.get_thread(thread_id)
    assert AgentRef(provider_key=thread["provider_key"], name=thread["agent_name"]) == agent_id


async def test_resume_keeps_the_same_run_id(souk, new_identity, attach):
    identity = new_identity()
    agent_id = await _register(souk, "echo", identity)
    await attach(identity, EchoProvider(), [agent_id.name])

    handle = await souk.start_run(agent_id, {"messages": [{"role": "user", "content": "one"}]})
    first_round = [e async for e in handle.events()]
    await _until(lambda: handle.run_id not in souk.active_runs())

    resumed = await souk.resume_run(
        handle.run_id, {"messages": [{"role": "user", "content": "two"}]}
    )
    assert resumed.run_id == handle.run_id
    second_round = [e async for e in resumed.events()]
    await _until(lambda: handle.run_id not in souk.active_runs())

    assert len(await souk.get_run_events(handle.run_id)) == len(first_round) + len(second_round)


async def test_resume_an_unknown_run_is_an_error(souk):
    with pytest.raises(LookupError):
        await souk.resume_run("run_nope", {"messages": []})


@pytest.fixture
async def own_souk(settings):
    instance = Souk(settings)
    try:
        yield instance
    finally:
        await instance.aclose()


async def test_start_reconciles_what_the_last_process_left_behind(own_souk, new_identity):
    agent_id = await _register(own_souk, "echo", new_identity())
    async with own_souk.session() as session:
        thread_id = await repo.create_thread(session, agent_id)
        stale = (await repo.create_run(session, thread_id, agent_id, "ag-ui", {"messages": []}))["run_id"]
        await session.commit()

    orphaned = await own_souk.start()

    assert orphaned == [stale]
    assert (await own_souk.get_run(stale)).status == "failed"


async def test_start_runs_once_so_a_second_call_cannot_reap_live_work(own_souk, new_identity):
    agent_id = await _register(own_souk, "echo", new_identity())
    await own_souk.start()

    async with own_souk.session() as session:
        thread_id = await repo.create_thread(session, agent_id)
        fresh = (await repo.create_run(session, thread_id, agent_id, "ag-ui", {"messages": []}))["run_id"]
        await session.commit()

    assert await own_souk.start() == []
    assert (await own_souk.get_run(fresh)).status == "queued"


async def test_start_keeps_exactly_one_sweeper_and_aclose_stops_it(own_souk):
    def sweepers():
        return [t for t in own_souk._tasks if t.get_name() == "health-sweeps" and not t.done()]

    await own_souk.start()
    await own_souk.start()
    assert len(sweepers()) == 1

    await own_souk.aclose()
    await _until(lambda: not sweepers())


async def test_aclose_without_start_is_fine(own_souk):
    assert not [t for t in own_souk._tasks if not t.done()]
