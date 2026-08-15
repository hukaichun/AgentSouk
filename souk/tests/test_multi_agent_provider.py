"""One provider, several agents.

A single process hosting a translator and a summarizer is ordinary, not a
special case — so a provider has to be able to tell which of its agents a run
is for. AG-UI's RunAgentInput carries thread and run ids but no agent
identity, so souk passes `agent_id` alongside it.

Before that, `start()` received only the run input: an in-process provider
attached to two agents got runs it could not distinguish, making it
effectively single-agent. The gRPC provider had papered over the same gap
with a private run_id -> agent_id side-table populated out of band, which was
the tell that the port was missing something rather than that gRPC was
special.
"""

from __future__ import annotations

import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from souk.identity import registration_signing_payload


class TwoAgentProvider:
    """Serves both agents, and answers as whichever one was asked."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def start(self, agent_id: str, run_input: dict):
        self.seen.append(agent_id)
        return self._events(agent_id, run_input)

    async def cancel(self, run_id: str) -> None:
        pass

    async def _events(self, agent_id: str, run_input: dict):
        yield {"type": "RUN_STARTED", "threadId": run_input["threadId"], "runId": run_input["runId"]}
        yield {"type": "TEXT_MESSAGE_START", "messageId": "m1", "role": "assistant"}
        yield {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": f"handled by {agent_id}"}
        yield {"type": "TEXT_MESSAGE_END", "messageId": "m1"}
        yield {"type": "RUN_FINISHED", "threadId": run_input["threadId"], "runId": run_input["runId"]}


async def test_one_provider_serves_several_agents(souk):
    key = Ed25519PrivateKey.generate()
    timestamp = int(time.time())
    names = ["translator", "summarizer"]
    registration = await souk.register_agents(
        "sdk_1",
        key.public_key().public_bytes_raw().hex(),
        key.sign(registration_signing_payload("sdk_1", names, timestamp)).hex(),
        timestamp,
        [{"name": n} for n in names],
    )
    translator = registration.agent_ids["translator"]
    summarizer = registration.agent_ids["summarizer"]

    provider = TwoAgentProvider()
    await souk.attach_provider(translator, provider)
    await souk.attach_provider(summarizer, provider)

    replies = {}
    for agent_id in (translator, summarizer):
        handle = await souk.start_run(agent_id, {"messages": []})
        events = [e async for e in handle.events()]
        replies[agent_id] = next(e["delta"] for e in events if e.get("delta"))

    # Each run reached the provider tagged with the agent it was for, and the
    # provider could answer accordingly.
    assert provider.seen == [translator, summarizer]
    assert replies[translator] == f"handled by {translator}"
    assert replies[summarizer] == f"handled by {summarizer}"


async def test_the_run_input_itself_still_carries_no_agent_id(souk):
    """Documents why the parameter exists: AG-UI's schema has no field for
    it, and souk does not smuggle one in — RunAgentInput stays exactly what
    the protocol says it is."""
    key = Ed25519PrivateKey.generate()
    timestamp = int(time.time())
    registration = await souk.register_agents(
        "sdk_1",
        key.public_key().public_bytes_raw().hex(),
        key.sign(registration_signing_payload("sdk_1", ["solo"], timestamp)).hex(),
        timestamp,
        [{"name": "solo"}],
    )
    agent_id = registration.agent_ids["solo"]

    seen: dict = {}

    class Recorder:
        async def start(self, agent_id: str, run_input: dict):
            seen["agent_id"] = agent_id
            seen["keys"] = set(run_input)
            return self._events(run_input)

        async def cancel(self, run_id: str) -> None:
            pass

        async def _events(self, run_input: dict):
            yield {"type": "RUN_FINISHED", "threadId": run_input["threadId"], "runId": run_input["runId"]}

    await souk.attach_provider(agent_id, Recorder())
    handle = await souk.start_run(agent_id, {"messages": []})
    [_ async for _ in handle.events()]

    assert seen["agent_id"] == agent_id
    assert not {"agentId", "agent_id"} & seen["keys"]
