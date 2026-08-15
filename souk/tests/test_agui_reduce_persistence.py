"""grpc_server._handle_finish now persists a completed/paused run's own
reply into thread_history (see souk/agui_reduce.py) — this is what makes
GET /threads/{thread_id} an actual source of truth for the full
conversation, not just the caller's half of it. End-to-end verified
against a real LLM (souk-guide's list_souk_agents tool call) manually;
this pins the same behavior against the real handlers with a synthetic
event stream.
"""

from __future__ import annotations

import json

from souk import repo
from souk.broker import FinishStream, RelayEvent
from souk.handlers import _handle_finish, _handle_relay


async def test_a_tool_call_reply_is_persisted_as_real_thread_history_messages(session, souk, new_identity):
    identity = new_identity()
    agent_ids = await repo.register_agents(session, identity.public_key, [{"name": "b"}])
    agent_b = agent_ids["b"]
    thread_b = await repo.create_thread(session, agent_b)
    created = await repo.create_run(session, thread_b, agent_b, "ag-ui", {})
    run_id = created["run_id"]
    await session.commit()

    run = souk.broker.enqueue_run(run_id, agent_b, thread_b, {}, "ag-ui")
    events = [
        {"type": "RUN_STARTED", "threadId": thread_b, "runId": run_id},
        {
            "type": "TOOL_CALL_START",
            "toolCallId": "call_1",
            "toolCallName": "list_souk_agents",
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
        await _handle_relay(souk, run, RelayEvent(event))
    await _handle_finish(souk, run, FinishStream())

    stored = await repo.get_thread_messages(session, thread_b)
    assert [m["role"] for m in stored] == ["assistant", "tool", "assistant"]
    assert stored[0]["toolCalls"][0]["function"]["name"] == "list_souk_agents"
    assert stored[1]["toolCallId"] == "call_1"
    assert stored[1]["content"] == "- b (online)"
    assert stored[2]["content"] == "Here you go."
    # Every id is database-generated — none of the reducer's synthetic
    # ids (m1/m2/tool_1) survive into the stored rows.
    assert all(m["id"].startswith("msg_") for m in stored)
    souk.broker.forget(run_id)


async def test_a_plain_text_only_reply_is_still_persisted(session, souk, new_identity):
    identity = new_identity()
    agent_ids = await repo.register_agents(session, identity.public_key, [{"name": "b"}])
    agent_b = agent_ids["b"]
    thread_b = await repo.create_thread(session, agent_b)
    created = await repo.create_run(session, thread_b, agent_b, "ag-ui", {})
    run_id = created["run_id"]
    await session.commit()

    run = souk.broker.enqueue_run(run_id, agent_b, thread_b, {}, "ag-ui")
    events = [
        {"type": "TEXT_MESSAGE_START", "messageId": "m1", "role": "assistant"},
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "hi"},
        {"type": "TEXT_MESSAGE_END", "messageId": "m1"},
        {"type": "RUN_FINISHED", "threadId": thread_b, "runId": run_id, "outcome": {"type": "success"}},
    ]
    for event in events:
        await _handle_relay(souk, run, RelayEvent(event))
    await _handle_finish(souk, run, FinishStream())

    stored = await repo.get_thread_messages(session, thread_b)
    assert len(stored) == 1
    assert stored[0]["role"] == "assistant"
    assert stored[0]["content"] == "hi"
    souk.broker.forget(run_id)


async def test_a_failed_run_persists_nothing_to_thread_history(session, souk, new_identity):
    """failed/cancelled replies are incomplete — kept in run_events
    (nothing deletes them) but deliberately never promoted into
    thread_history's conversational record — see the design discussion
    this pins: not every terminal status should produce a message.
    """
    from souk.broker import Fail
    from souk.handlers import _handle_fail

    identity = new_identity()
    agent_ids = await repo.register_agents(session, identity.public_key, [{"name": "b"}])
    agent_b = agent_ids["b"]
    thread_b = await repo.create_thread(session, agent_b)
    created = await repo.create_run(session, thread_b, agent_b, "ag-ui", {})
    run_id = created["run_id"]
    await session.commit()

    run = souk.broker.enqueue_run(run_id, agent_b, thread_b, {}, "ag-ui")
    partial = {"type": "TEXT_MESSAGE_START", "messageId": "m1", "role": "assistant"}
    await _handle_relay(souk, run, RelayEvent(partial))
    await _handle_fail(souk, run, Fail(reason="stalled"))

    stored = await repo.get_thread_messages(session, thread_b)
    assert stored == []
    souk.broker.forget(run_id)
