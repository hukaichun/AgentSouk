"""souk used as a library: attach an agent, start a run, ask what happened.

Everything here goes through `Souk`'s own methods — no repo calls, no broker
access, no HTTP client, no ports bound. That is the point of the facade: an
embedding caller shouldn't need to know souk's internals exist, and if these
tests ever need to reach past `souk.` to do something ordinary, the facade is
missing a method.
"""

from __future__ import annotations

import asyncio

import pytest

from souk import repo


class EchoProvider:
    """An in-process provider, in the shape souk's port asks for: told which
    agent each run is for, streaming AG-UI events back."""

    async def run_stream(self, agent_id: str, run_input: dict):
        text = run_input["messages"][-1]["content"] if run_input.get("messages") else ""
        yield {"type": "RUN_STARTED", "threadId": run_input["threadId"], "runId": run_input["runId"]}
        yield {"type": "TEXT_MESSAGE_START", "messageId": "m1", "role": "assistant"}
        yield {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": f"echo: {text}"}
        yield {"type": "TEXT_MESSAGE_END", "messageId": "m1"}
        yield {"type": "RUN_FINISHED", "threadId": run_input["threadId"], "runId": run_input["runId"]}


class NeverFinishesProvider:
    """Starts, then waits forever. Ends without RUN_FINISHED when its worker
    stops it — the only way AG-UI can express "stopped without finishing"
    (see handlers._handle_finish)."""

    async def run_stream(self, agent_id: str, run_input: dict):
        yield {"type": "RUN_STARTED", "threadId": run_input["threadId"], "runId": run_input["runId"]}
        await asyncio.Event().wait()


async def _register(souk, name: str, identity) -> str:
    async with souk.session() as session:
        agent_ids = await repo.register_agents(session, identity.public_key, [{"name": name}])
    return agent_ids[name]


async def _register_with_token(souk, name: str, identity):
    """Registration through souk's own door, which is the only one that
    hands back a session token — what a worker needs to claim with."""
    body = identity.register_body([{"name": name}])
    return await souk.register_agents(
        body["public_key"], body["signature"], body["timestamp"], body["agents"]
    )


async def _until(predicate, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


async def test_attach_start_and_read_back(souk, new_identity):
    identity = new_identity()
    agent_id = await _register(souk, "echo", identity)
    await souk.attach_provider(identity.public_key, EchoProvider(), [agent_id])

    handle = await souk.start_run(agent_id, {"messages": [{"role": "user", "content": "hi"}]})

    # The ids are available immediately, without consuming the stream —
    # what A2A's tasks/send and tasks/get need.
    assert handle.run_id.startswith("run_")
    assert handle.thread_id.startswith("thread_")
    assert handle.is_live

    events = [event async for event in handle.events()]
    assert [e["type"] for e in events][0] == "RUN_STARTED"
    assert any(e.get("delta") == "echo: hi" for e in events)

    await _until(lambda: handle.run_id not in souk.active_runs())

    run = await souk.get_run(handle.run_id)
    assert run["status"] == "completed"

    # The reply was persisted as real thread history, not just relayed.
    messages = await souk.get_thread_messages(handle.thread_id)
    assert messages[-1]["content"] == "echo: hi"

    # And the run's own event log is queryable after the fact.
    assert len(await souk.get_run_events(handle.run_id)) == len(events)


async def test_roster_and_agent_lookup(souk, new_identity):
    agent_id = await _register(souk, "echo", new_identity())

    roster = await souk.list_agents()
    assert [a["name"] for a in roster] == ["echo"]
    assert roster[0]["online"] is True

    assert (await souk.get_agent(agent_id))["name"] == "echo"
    assert [a["agent_id"] for a in await souk.resolve_agents_by_name("echo")] == [agent_id]
    assert await souk.get_agent("agent_nope") is None


async def test_cancel_a_running_agent(souk, new_identity):
    identity = new_identity()
    agent_id = await _register(souk, "slow", identity)
    await souk.attach_provider(identity.public_key, NeverFinishesProvider(), [agent_id])

    handle = await souk.start_run(agent_id, {"messages": []})
    # Read the agent's first event before cancelling, so a worker
    # demonstrably has the run — otherwise this races the claim and would
    # silently exercise the never-handed-over path instead (which
    # test_a_cancel_before_any_provider_takes_the_run covers on purpose).
    stream = handle.events()
    assert (await anext(stream))["type"] == "RUN_STARTED"

    assert souk.cancel_run(handle.run_id) is True
    assert [e async for e in stream] == []  # honoured it, stopped producing
    await _until(lambda: handle.run_id not in souk.active_runs())

    run = await souk.get_run(handle.run_id)
    assert run["status"] == "cancelled"

    # Cancelling something souk isn't dispatching is reported, not raised.
    assert souk.cancel_run(handle.run_id) is False


async def test_a_worker_that_ignores_the_cancel_still_completes(souk, new_identity):
    """souk asks; it does not compel. If the agent finishes anyway, the run
    completed — recording `cancelled` there would be souk claiming something
    it never verified, and the run's own output would contradict it.

    Driven through the claim/report methods rather than through souk's own
    worker, because complying is a *worker's* decision (souk's in-process
    one cancels the agent's task, as the reference SDK does remotely) and
    this is about what core does with a worker that decides otherwise.
    """
    identity = new_identity()
    registration = await _register_with_token(souk, "stubborn", identity)
    agent_id = registration.agent_ids["stubborn"]
    # This "worker" is the identity that claimed — its key, nothing else.

    handle = await souk.start_run(agent_id, {"messages": []})
    claimed = await souk.claim_work(registration.session_token, [agent_id])
    assert [c.run_id for c in claimed] == [handle.run_id]

    souk.cancel_run(handle.run_id)
    # Cancel requested, and this worker carries on regardless — souk keeps
    # persisting and relaying what it produces, because that is real output.
    souk.report_event(
        handle.run_id,
        {"type": "RUN_FINISHED", "threadId": handle.thread_id, "runId": handle.run_id},
        claimed_by=identity.public_key,
    )
    souk.finish_run(handle.run_id, claimed_by=identity.public_key)

    events = [e async for e in handle.events()]
    await _until(lambda: handle.run_id not in souk.active_runs())

    assert events[-1]["type"] == "RUN_FINISHED"
    assert (await souk.get_run(handle.run_id))["status"] == "completed"


async def test_a_cancel_before_any_provider_takes_the_run(souk, new_identity):
    """Nothing was ever handed over, so souk is the only party involved and
    can record the outcome outright — no provider to ask, nothing to wait
    for."""
    agent_id = await _register(souk, "never-claimed", new_identity())
    # Deliberately no attach_provider: nobody will ever claim this run.
    handle = await souk.start_run(agent_id, {"messages": []})

    assert souk.cancel_run(handle.run_id) is True
    await _until(lambda: handle.run_id not in souk.active_runs())

    assert (await souk.get_run(handle.run_id))["status"] == "cancelled"


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


async def test_start_run_reuses_an_existing_thread(souk, new_identity):
    identity = new_identity()
    agent_id = await _register(souk, "echo", identity)
    await souk.attach_provider(identity.public_key, EchoProvider(), [agent_id])

    thread_id = await souk.create_thread(agent_id)
    first = await souk.start_run(agent_id, {"messages": []}, thread_id=thread_id)
    assert first.thread_id == thread_id
    [_ async for _ in first.events()]

    assert (await souk.get_thread(thread_id))["agent_id"] == agent_id


async def test_resume_keeps_the_same_run_id(souk, new_identity):
    """A run's identity is stable across pause/resume rounds — that is what
    lets a caller's task id keep pointing at the same task for its whole
    life instead of chasing a chain of new ids."""
    identity = new_identity()
    agent_id = await _register(souk, "echo", identity)
    await souk.attach_provider(identity.public_key, EchoProvider(), [agent_id])

    handle = await souk.start_run(agent_id, {"messages": [{"role": "user", "content": "one"}]})
    first_round = [e async for e in handle.events()]
    await _until(lambda: handle.run_id not in souk.active_runs())

    resumed = await souk.resume_run(
        handle.run_id, {"messages": [{"role": "user", "content": "two"}]}
    )
    assert resumed.run_id == handle.run_id
    second_round = [e async for e in resumed.events()]
    await _until(lambda: handle.run_id not in souk.active_runs())

    # The second round's events continue the same log rather than colliding
    # with the first round's sequence numbers.
    assert len(await souk.get_run_events(handle.run_id)) == len(first_round) + len(second_round)


async def test_resume_an_unknown_run_is_an_error(souk):
    with pytest.raises(LookupError):
        await souk.resume_run("run_nope", {"messages": []})
