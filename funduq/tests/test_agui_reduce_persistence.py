from __future__ import annotations

import json

from funduq import repo
from funduq.broker import FinishStream, RelayEvent
from funduq.handlers import _handle_finish, _handle_relay


async def test_a_tool_call_reply_is_persisted_as_real_thread_history_messages(session, funduq, new_identity):
    identity = new_identity()
    registered = await repo.register_agents(session, identity.public_key, [{"name": "b"}])
    agent_b = registered["b"]
    thread_b = await repo.create_thread(session, agent_b)
    created = await repo.create_run(session, thread_b, agent_b, "ag-ui", {})
    await repo.mark_run_status(session, created["run_id"], "running")
    run_id = created["run_id"]
    await session.commit()

    run = funduq.broker.enqueue_run(run_id, agent_b, thread_b, {}, "ag-ui")
    events = [
        {"type": "RUN_STARTED", "threadId": thread_b, "runId": run_id},
        {
            "type": "TOOL_CALL_START",
            "toolCallId": "call_1",
            "toolCallName": "list_funduq_agents",
            "parentMessageId": "m1",
        },
        {"type": "TOOL_CALL_ARGS", "toolCallId": "call_1", "delta": "{}"},
        {"type": "TOOL_CALL_END", "toolCallId": "call_1"},
        {
            "type": "TOOL_CALL_RESULT",
            "messageId": "tool_1",
            "toolCallId": "call_1",
            "content": "- b (online)",
        },
        {"type": "TEXT_MESSAGE_START", "messageId": "m2", "role": "assistant"},
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m2", "delta": "Here you go."},
        {"type": "TEXT_MESSAGE_END", "messageId": "m2"},
        {"type": "RUN_FINISHED", "threadId": thread_b, "runId": run_id, "outcome": {"type": "success"}},
    ]
    for event in events:
        await _handle_relay(funduq, run, RelayEvent(event))
    await _handle_finish(funduq, run, FinishStream())

    stored = await repo.get_thread_messages(session, thread_b)
    assert [m["role"] for m in stored] == ["assistant", "tool", "assistant"]
    assert stored[0]["toolCalls"][0]["function"]["name"] == "list_funduq_agents"
    assert stored[1]["toolCallId"] == "call_1"
    assert stored[1]["content"] == "- b (online)"
    assert stored[2]["content"] == "Here you go."
    assert all(m["id"].startswith("msg_") for m in stored)
    funduq.broker.forget(run_id)


async def test_a_plain_text_only_reply_is_still_persisted(session, funduq, new_identity):
    identity = new_identity()
    registered = await repo.register_agents(session, identity.public_key, [{"name": "b"}])
    agent_b = registered["b"]
    thread_b = await repo.create_thread(session, agent_b)
    created = await repo.create_run(session, thread_b, agent_b, "ag-ui", {})
    await repo.mark_run_status(session, created["run_id"], "running")
    run_id = created["run_id"]
    await session.commit()

    run = funduq.broker.enqueue_run(run_id, agent_b, thread_b, {}, "ag-ui")
    events = [
        {"type": "TEXT_MESSAGE_START", "messageId": "m1", "role": "assistant"},
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "hi"},
        {"type": "TEXT_MESSAGE_END", "messageId": "m1"},
        {"type": "RUN_FINISHED", "threadId": thread_b, "runId": run_id, "outcome": {"type": "success"}},
    ]
    for event in events:
        await _handle_relay(funduq, run, RelayEvent(event))
    await _handle_finish(funduq, run, FinishStream())

    stored = await repo.get_thread_messages(session, thread_b)
    assert len(stored) == 1
    assert stored[0]["role"] == "assistant"
    assert stored[0]["content"] == "hi"
    funduq.broker.forget(run_id)


async def test_a_failed_run_persists_nothing_to_thread_history(session, funduq, new_identity):
    from funduq.broker import Fail
    from funduq.handlers import _handle_fail

    identity = new_identity()
    registered = await repo.register_agents(session, identity.public_key, [{"name": "b"}])
    agent_b = registered["b"]
    thread_b = await repo.create_thread(session, agent_b)
    created = await repo.create_run(session, thread_b, agent_b, "ag-ui", {})
    await repo.mark_run_status(session, created["run_id"], "running")
    run_id = created["run_id"]
    await session.commit()

    run = funduq.broker.enqueue_run(run_id, agent_b, thread_b, {}, "ag-ui")
    partial = {"type": "TEXT_MESSAGE_START", "messageId": "m1", "role": "assistant"}
    await _handle_relay(funduq, run, RelayEvent(partial))
    await _handle_fail(funduq, run, Fail(reason="stalled"))

    stored = await repo.get_thread_messages(session, thread_b)
    assert stored == []
    funduq.broker.forget(run_id)
