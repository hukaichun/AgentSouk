"""AGUIAdapter, exercised.

This file exists because the adapter had none. Both of its methods were dead
— `resolve_agent_id` raised `KeyError('agent')` on every call and `run` died
on line one calling a `repo` function that does not exist — while 220 tests
stayed green, because nothing imported the class. Its A2A sibling has been
covered all along, which is why only this half rotted.

So the point of these is coverage of the seam, not of the run machinery
underneath: reaching `run` at all is what was missing.
"""

from __future__ import annotations

import asyncio

import pytest
from ag_ui.core import RunAgentInput, UserMessage

from souk.errors import AgentNotFound
from souk.models import AgentRef
from souk.protocols.agui import AGUIAdapter, EventStream, ThreadSnapshot


def _body(thread_id: str = "t-1", text: str = "hi") -> RunAgentInput:
    return RunAgentInput(
        thread_id=thread_id,
        # Never used: souk mints its own. Present because the schema demands it.
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
            "threadId": run_input["threadId"],
            "runId": run_input["runId"],
        }
        await asyncio.Event().wait()


async def test_run_reaches_a_served_agent(souk, serve):
    served = await serve(None, "solo")

    result = await AGUIAdapter(souk).run(served.agents["solo"], _body())

    assert isinstance(result, EventStream)
    assert [e["type"] async for e in result.events] == [
        "RUN_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]


async def test_the_callers_run_id_is_ignored(souk, serve):
    served = await serve(None, "solo")

    result = await AGUIAdapter(souk).run(served.agents["solo"], _body())

    assert result.run_id != "whatever-the-caller-sent"


async def test_an_unseen_thread_id_is_a_new_conversation_not_an_error(souk, serve):
    """AG-UI's threadId is minted by the caller and there is no "create
    thread" call, so an id souk has never seen cannot be a caller error."""
    served = await serve(None, "solo")

    result = await AGUIAdapter(souk).run(served.agents["solo"], _body("never-seen-before"))

    assert isinstance(result, EventStream)
    # souk mints a real thread rather than trusting the caller's string.
    assert result.thread_id != "never-seen-before"
    assert await souk.get_thread(result.thread_id) is not None


async def test_an_unregistered_agent_says_which_one(souk):
    """The ref reaches the message. It used to be rebound to the lookup's
    result before the None check, so this always read `agent 'None'`."""
    ghost = AgentRef(provider_key="deadbeef" * 8, name="ghost")

    with pytest.raises(AgentNotFound) as raised:
        await AGUIAdapter(souk).run(ghost, _body())

    assert "ghost" in str(raised.value)
    assert "None" not in str(raised.value)


async def test_a_registered_but_unserved_agent_fails_the_run_rather_than_hanging(souk, register):
    """Registered is not reachable. Nobody is attached, so souk should close
    the stream with a terminal event instead of opening one that idles."""
    registered = await register("orphan")

    result = await AGUIAdapter(souk).run(registered.agents["orphan"], _body())

    assert isinstance(result, EventStream)
    assert [e["type"] async for e in result.events][-1] == "RUN_ERROR"
    assert (await souk.get_run(result.run_id)).status == "failed"


async def test_a_second_run_on_a_busy_thread_gets_the_thread_back(souk, serve):
    """One active run per thread — a concurrent second would fork an
    otherwise-linear history. souk hands back the thread's state rather than
    erroring or quietly queueing a duplicate."""
    served = await serve(_NeverFinishes(), "slow")
    adapter = AGUIAdapter(souk)

    first = await adapter.run(served.agents["slow"], _body("t-busy"))
    assert isinstance(first, EventStream)
    assert (await anext(first.events))["type"] == "RUN_STARTED"

    second = await adapter.run(served.agents["slow"], _body(first.thread_id))

    assert isinstance(second, ThreadSnapshot)
    assert second.data["active_run"]["run_id"] == first.run_id


async def test_events_encode_as_sse_payloads(souk, serve):
    """`encode` is part of speaking AG-UI, so it ships with the adapter
    rather than with whoever frames the HTTP response."""
    served = await serve(None, "solo")

    result = await AGUIAdapter(souk).run(served.agents["solo"], _body())

    payloads = [p async for p in result.encode()]
    assert all(isinstance(p, str) for p in payloads)
    assert payloads[0].startswith("{") and '"RUN_STARTED"' in payloads[0]
