from __future__ import annotations

import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from souk.errors import AgentNotFound
from souk_provider_sdk import ProviderIdentity


class TwoAgentProvider:

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def run_stream(self, agent_id: str, run_input):
        self.seen.append(agent_id)
        yield {"type": "RUN_STARTED", "threadId": run_input.thread_id, "runId": run_input.run_id}
        yield {"type": "TEXT_MESSAGE_START", "messageId": "m1", "role": "assistant"}
        yield {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": f"handled by {agent_id}"}
        yield {"type": "TEXT_MESSAGE_END", "messageId": "m1"}
        yield {"type": "RUN_FINISHED", "threadId": run_input.thread_id, "runId": run_input.run_id}


async def _register(souk, *names: str):
    identity = ProviderIdentity.generate()
    signature, timestamp = identity.sign_registration(list(names))
    registration = await souk.register_agents(
        identity.public_key, signature, timestamp, [{"name": n} for n in names]
    )
    return registration, identity


async def test_one_provider_serves_several_agents(souk, attach):
    registration, identity = await _register(souk, "translator", "summarizer")
    translator = registration.agents["translator"]
    summarizer = registration.agents["summarizer"]

    provider = TwoAgentProvider()
    await attach(identity, provider, [translator.name, summarizer.name])

    replies = {}
    for agent_id in (translator, summarizer):
        handle = await souk.start_run(agent_id, {"messages": []})
        events = [e async for e in handle.events()]
        replies[agent_id] = next(e["delta"] for e in events if e.get("delta"))

    assert provider.seen == [translator.name, summarizer.name]
    assert replies[translator] == f"handled by {translator.name}"
    assert replies[summarizer] == f"handled by {summarizer.name}"


async def test_the_run_input_itself_still_carries_no_agent_id(souk, attach):
    registration, identity = await _register(souk, "solo")
    agent_id = registration.agents["solo"]
    seen: dict = {}

    class Recorder:
        async def run_stream(self, agent_id: str, run_input):
            seen["agent_name"] = agent_id
            seen["keys"] = set(run_input.model_dump(by_alias=True))
            yield {
                "type": "RUN_FINISHED",
                "threadId": run_input.thread_id,
                "runId": run_input.run_id,
            }

    await attach(identity, Recorder(), [agent_id.name])
    handle = await souk.start_run(agent_id, {"messages": []})
    [_ async for _ in handle.events()]

    assert seen["agent_name"] == agent_id.name
    assert not {"agentId", "agent_id"} & seen["keys"]


async def test_a_provider_cannot_attach_an_agent_it_did_not_register(souk, attach):
    _mine, identity = await _register(souk, "mine")
    await _register(souk, "theirs")

    with pytest.raises(AgentNotFound):
        await attach(identity, TwoAgentProvider(), ["mine", "theirs"])
