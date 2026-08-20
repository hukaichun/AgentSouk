"""A message sent while a run is active is queued and handled next — never dropped.

The A2A door used to answer a mid-run message with the in-flight task and
silently discard the message. These tests pin the two lanes that replaced
that: an ordinary utterance becomes a new queued run dispatched after the
active one (one turn per thread at a time), and a message addressed via
`taskId` to the thread's paused `input-required` task resumes that task.
"""

from __future__ import annotations

import asyncio

from souk import repo
from souk.protocols.a2a import A2AAdapter


async def _rpc(souk, agent, method: str, params: dict):
    return await A2AAdapter(souk).handle_rpc(
        agent, {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}
    )


def _message(text: str) -> dict:
    return {"role": "user", "parts": [{"type": "text", "text": text}]}


async def _until(predicate, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while True:
            result = predicate()
            if asyncio.iscoroutine(result):
                result = await result
            if result:
                return
            await asyncio.sleep(0.01)


class GateAgent:
    """Holds every run open until `release` is set, recording what it was handed."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.runs: list = []

    async def run_stream(self, agent_name: str, run_input):
        self.runs.append(run_input)
        ids = {"threadId": run_input.thread_id, "runId": run_input.run_id}
        yield {"type": "RUN_STARTED", **ids}
        await self.release.wait()
        yield {"type": "RUN_FINISHED", **ids}


class AskingAgent:
    """Pauses its first round on an interrupt; any later round completes."""

    def __init__(self) -> None:
        self.rounds: list = []

    async def run_stream(self, agent_name: str, run_input):
        self.rounds.append(run_input)
        ids = {"threadId": run_input.thread_id, "runId": run_input.run_id}
        yield {"type": "RUN_STARTED", **ids}
        if len(self.rounds) == 1:
            yield {
                "type": "RUN_FINISHED",
                **ids,
                "outcome": {
                    "type": "interrupt",
                    "interrupts": [{"id": "int_1", "reason": "question", "message": "which?"}],
                },
            }
        else:
            yield {"type": "RUN_FINISHED", **ids}


async def test_a_message_sent_mid_run_is_queued_not_dropped(souk, serve):
    provider = GateAgent()
    served = await serve(provider, "busy")
    agent = served.agents["busy"]

    first = asyncio.create_task(
        _rpc(souk, agent, "SendMessage", {"message": _message("start working")})
    )
    await _until(lambda: len(provider.runs) == 1)
    thread_id = provider.runs[0].thread_id

    second = asyncio.create_task(
        _rpc(
            souk,
            agent,
            "SendMessage",
            {"message": {**_message("one more thing"), "contextId": thread_id}},
        )
    )

    async def _second_is_queued() -> bool:
        async with souk.session() as session:
            messages = await repo.get_thread_messages(session, thread_id)
            snapshot = await repo.get_thread_snapshot(session, thread_id)
        texts = [m.get("content") for m in messages]
        return "one more thing" in texts and snapshot["active_run"] is not None

    await _until(_second_is_queued)
    assert len(provider.runs) == 1, "the second message must wait its turn, not join mid-run"

    provider.release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result["result"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert second_result["result"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert second_result["result"]["id"] != first_result["result"]["id"]
    assert second_result["result"]["contextId"] == thread_id
    assert [r.thread_id for r in provider.runs] == [thread_id, thread_id]


async def test_a_reply_addressed_to_the_paused_task_resumes_it(souk, serve):
    provider = AskingAgent()
    served = await serve(provider, "asker")
    agent = served.agents["asker"]

    first = await _rpc(souk, agent, "SendMessage", {"message": _message("do the thing")})
    task_id = first["result"]["id"]
    assert first["result"]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"

    second = await _rpc(
        souk,
        agent,
        "SendMessage",
        {"message": {**_message("the answer"), "taskId": task_id}},
    )

    assert second["result"]["id"] == task_id, "a reply resumes the task, not a new one"
    assert second["result"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert len(provider.rounds) == 2
    assert provider.rounds[1].run_id == provider.rounds[0].run_id


async def test_an_unaddressed_message_waits_for_the_paused_task(souk, serve):
    provider = AskingAgent()
    served = await serve(provider, "patient")
    agent = served.agents["patient"]

    first = await _rpc(souk, agent, "SendMessage", {"message": _message("do the thing")})
    task_id = first["result"]["id"]
    thread_id = first["result"]["contextId"]

    unaddressed = asyncio.create_task(
        _rpc(
            souk,
            agent,
            "SendMessage",
            {"message": {**_message("also, unrelated"), "contextId": thread_id}},
        )
    )

    async def _it_is_queued() -> bool:
        async with souk.session() as session:
            latest_active = await repo.get_active_run_for_thread(session, thread_id)
        return latest_active is not None and latest_active["status"] == "queued"

    await _until(_it_is_queued)
    await asyncio.sleep(0.1)
    assert len(provider.rounds) == 1, "the paused question holds the thread; nothing overtakes it"

    reply = await _rpc(
        souk, agent, "SendMessage", {"message": {**_message("the answer"), "taskId": task_id}}
    )
    assert reply["result"]["status"]["state"] == "TASK_STATE_COMPLETED"

    third = await unaddressed
    assert third["result"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert third["result"]["id"] != task_id
    assert len(provider.rounds) == 3


async def test_reopen_run_refuses_a_run_that_is_not_in_the_expected_status(session, new_identity):
    identity = new_identity()
    registered = await repo.register_agents(session, identity.public_key, [{"name": "r"}])
    agent = registered["r"]
    thread_id = await repo.create_thread(session, agent)
    created = await repo.create_run(session, thread_id, agent, "ag-ui", {})
    await session.commit()

    reopened = await repo.reopen_run(
        session, created["run_id"], {}, expected_status="input-required"
    )

    assert reopened is False
    stored = await repo.get_run(session, created["run_id"])
    assert stored.status == "queued"


async def test_start_reseeds_the_thread_gate_from_paused_runs(settings, session, new_identity):
    from souk.core import Souk

    identity = new_identity()
    registered = await repo.register_agents(session, identity.public_key, [{"name": "s"}])
    agent = registered["s"]
    thread_id = await repo.create_thread(session, agent)
    created = await repo.create_run(session, thread_id, agent, "ag-ui", {})
    await session.commit()
    await repo.mark_run_status(session, created["run_id"], "input-required")

    reborn = Souk(settings)
    try:
        await reborn.start()
        assert reborn.broker._thread_holder.get(thread_id) == created["run_id"]
    finally:
        await reborn.aclose()


async def test_an_agui_run_queued_behind_another_flows_when_its_turn_comes(souk, serve):
    from ag_ui.core import RunAgentInput, UserMessage

    from souk.protocols.agui import AGUIAdapter

    def _body(thread_id: str, text: str) -> RunAgentInput:
        return RunAgentInput(
            thread_id=thread_id,
            run_id="ignored",
            state={},
            messages=[UserMessage(id="m1", role="user", content=text)],
            tools=[],
            context=[],
            forwarded_props={},
        )

    async def _drain(stream) -> list[dict]:
        return [event async for event in stream.events]

    provider = GateAgent()
    served = await serve(provider, "sse")
    agent = served.agents["sse"]
    adapter = AGUIAdapter(souk)

    first = await adapter.run(agent, _body("t-sse", "start"))
    first_events = asyncio.create_task(_drain(first))
    await _until(lambda: len(provider.runs) == 1)

    second = await adapter.run(agent, _body(first.thread_id, "one more"))
    second_events = asyncio.create_task(_drain(second))
    await asyncio.sleep(0.1)
    assert len(provider.runs) == 1, "the queued run must not start while the first is in flight"
    assert not second_events.done(), "its stream stays open, silent until its turn"

    provider.release.set()
    assert {e["type"] for e in await first_events} >= {"RUN_STARTED", "RUN_FINISHED"}
    assert {e["type"] for e in await second_events} >= {"RUN_STARTED", "RUN_FINISHED"}
    assert [r.thread_id for r in provider.runs] == [first.thread_id, first.thread_id]
