from __future__ import annotations

import asyncio

import pytest
from ag_ui.core import RunAgentInput, UserMessage

from funduq.errors import AgentNotFound
from funduq.models import AgentRef
from funduq.protocols.agui import AGUIAdapter, EventStream, ThreadSnapshot


def _body(thread_id: str = "t-1", text: str = "hi") -> RunAgentInput:
    return RunAgentInput(
        thread_id=thread_id,
        run_id="whatever-the-caller-sent",
        state={},
        messages=[UserMessage(id="m1", role="user", content=text)],
        tools=[],
        context=[],
        forwarded_props={},
    )


class _NeverFinishes:
    async def run_stream(self, agent_name: str, run_input: dict):
        yield {
            "type": "RUN_STARTED",
            "threadId": run_input.thread_id,
            "runId": run_input.run_id,
        }
        await asyncio.Event().wait()


async def test_run_reaches_a_served_agent(funduq, serve):
    served = await serve(None, "solo")

    result = await AGUIAdapter(funduq).run(served.agents["solo"], _body())

    assert isinstance(result, EventStream)
    assert [e["type"] async for e in result.events] == [
        "RUN_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]


async def test_the_callers_run_id_is_ignored(funduq, serve):
    served = await serve(None, "solo")

    result = await AGUIAdapter(funduq).run(served.agents["solo"], _body())

    assert result.run_id != "whatever-the-caller-sent"


async def test_an_unseen_thread_id_is_a_new_conversation_not_an_error(funduq, serve):
    served = await serve(None, "solo")

    result = await AGUIAdapter(funduq).run(served.agents["solo"], _body("never-seen-before"))

    assert isinstance(result, EventStream)
    assert result.thread_id != "never-seen-before"
    assert await funduq.get_thread(result.thread_id) is not None


async def test_an_unregistered_agent_says_which_one(funduq):
    ghost = AgentRef(provider_key="deadbeef" * 8, name="ghost")

    with pytest.raises(AgentNotFound) as raised:
        await AGUIAdapter(funduq).run(ghost, _body())

    assert "ghost" in str(raised.value)
    assert "None" not in str(raised.value)


async def test_a_registered_but_unserved_agent_fails_the_run_rather_than_hanging(funduq, register):
    registered = await register("orphan")

    result = await AGUIAdapter(funduq).run(registered.agents["orphan"], _body())

    assert isinstance(result, EventStream)
    assert [e["type"] async for e in result.events][-1] == "RUN_ERROR"
    assert (await funduq.get_run(result.run_id)).status == "failed"


async def test_a_second_run_on_a_busy_thread_is_queued_behind_the_first(funduq, serve):
    from funduq import repo

    served = await serve(_NeverFinishes(), "slow")
    adapter = AGUIAdapter(funduq)

    first = await adapter.run(served.agents["slow"], _body("t-busy"))
    assert isinstance(first, EventStream)
    assert (await anext(first.events))["type"] == "RUN_STARTED"

    second = await adapter.run(served.agents["slow"], _body(first.thread_id, "one more"))

    assert isinstance(second, EventStream), "a second run is accepted and queued, not refused"
    assert second.run_id != first.run_id
    async with funduq.session() as session:
        stored = await repo.get_run(session, second.run_id)
        messages = await repo.get_thread_messages(session, first.thread_id)
    assert stored.status == "queued", "it waits its turn behind the in-flight run"
    assert "one more" in [m.get("content") for m in messages], "and its message is kept"


async def test_events_encode_as_sse_payloads(funduq, serve):
    served = await serve(None, "solo")

    result = await AGUIAdapter(funduq).run(served.agents["solo"], _body())

    payloads = [p async for p in result.encode()]
    assert all(isinstance(p, str) for p in payloads)
    assert payloads[0].startswith("{") and '"RUN_STARTED"' in payloads[0]


async def test_a_provider_can_read_the_history_its_run_input_does_not_carry(funduq, serve):
    served = await serve(None, "solo")
    agent = served.agents["solo"]
    adapter = AGUIAdapter(funduq)

    first = await adapter.run(agent, _body("t-1", "one"))
    [_ async for _ in first.events]
    second = await adapter.run(agent, _body(first.thread_id, "two"))
    [_ async for _ in second.events]

    history = await served.runtime.link.thread_messages(first.thread_id)

    assert [m.role for m in history] == ["user", "assistant", "user", "assistant"]
    assert [m.content for m in history if m.role == "user"] == ["one", "two"]


async def test_limit_keeps_the_most_recent(funduq, serve):
    served = await serve(None, "solo")
    agent = served.agents["solo"]
    adapter = AGUIAdapter(funduq)

    thread_id = None
    for text in ("one", "two", "three"):
        result = await adapter.run(agent, _body(thread_id or "t-1", text))
        thread_id = result.thread_id
        [_ async for _ in result.events]

    assert len(await served.runtime.link.thread_messages(thread_id)) == 6
    assert [m.content for m in await served.runtime.link.thread_messages(thread_id, limit=2)] == ["three", "done"]


async def test_an_unknown_thread_is_empty_rather_than_an_error(funduq, serve):
    served = await serve(None, "solo")

    assert await served.runtime.link.thread_messages("no-such-thread") == []
