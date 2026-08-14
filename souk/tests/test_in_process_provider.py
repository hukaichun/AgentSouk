"""A run driven end to end by a local agent, with no socket anywhere.

This is what the AgentProvider port buys. Before it, the only way to have an
agent was a gRPC-connected one: the run handlers built protobuf envelopes
directly, so there was no way to drive a run without a transport. Here the
"provider" is a plain object with a `run` method — no registration over HTTP,
no AgentSession, no ports bound — and the same broker, the same handlers and
the same persistence carry it through.

The provider below is deliberately written the way an AG-UI agent already is
(`run_input` in, AG-UI events out), because that *is* the port — see
souk/providers.py.
"""

from __future__ import annotations

import asyncio

from souk import repo
from souk.broker import Claim, drain_run
from souk.handlers import make_handlers


class LocalAgent:
    """An agent that runs in this process. Satisfies AgentProvider by
    having the shape an AG-UI agent already has — nothing souk-specific."""

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.seen_input: dict | None = None

    async def start(self, run_input: dict):
        self.seen_input = run_input
        return self._events(run_input)

    async def _events(self, run_input: dict):
        yield {"type": "RUN_STARTED", "threadId": run_input["threadId"], "runId": run_input["runId"]}
        yield {"type": "TEXT_MESSAGE_START", "messageId": "m1", "role": "assistant"}
        yield {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": self._reply}
        yield {"type": "TEXT_MESSAGE_END", "messageId": "m1"}
        yield {"type": "RUN_FINISHED", "threadId": run_input["threadId"], "runId": run_input["runId"]}


async def test_a_local_agent_can_drive_a_run_with_no_transport(session, souk, new_identity):
    identity = new_identity()
    agent_ids = await repo.register_agents(session, "sdk_local", identity.public_key, [{"name": "local"}])
    agent_id = agent_ids["local"]
    thread_id = await repo.create_thread(session, agent_id)
    created = await repo.create_run(session, thread_id, agent_id, "ag-ui", {"messages": []})
    run_id = created["run_id"]

    agent = LocalAgent("hello from in-process")
    run = souk.broker.enqueue_run(
        run_id,
        agent_id,
        thread_id,
        {"threadId": thread_id, "runId": run_id, "messages": []},
        "ag-ui",
        make_handlers(souk),
    )

    # Claiming is the same act it is for a remote agent; only the thing
    # doing the claiming differs.
    run.in_queue.put_nowait(Claim(agent))

    events = [event async for event in drain_run(run)]

    assert [e["type"] for e in events] == [
        "RUN_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
    # The agent really was handed the run's input, not just started.
    assert agent.seen_input["runId"] == run_id

    # And souk persisted it exactly as it would for a remote agent: the run
    # completes, and the reply is reduced into real thread history.
    await _until(lambda: souk.broker.get(run_id) is None)
    stored = await repo.get_run(session, run_id)
    assert stored["status"] == "completed"
    messages = await repo.get_thread_messages(session, thread_id)
    assert messages[-1]["content"] == "hello from in-process"


async def _until(predicate, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)
