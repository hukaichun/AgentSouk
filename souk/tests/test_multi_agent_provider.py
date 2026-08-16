"""One provider, several agents.

A single process hosting a translator and a summarizer is ordinary, not a
special case — so a provider has to be able to tell which of its agents a run
is for. AG-UI's RunAgentInput carries thread and run ids but no agent
identity, so souk passes `agent_id` alongside it, on the port's own method.

Before that, a provider received only the run input: one attached to two
agents got runs it could not distinguish, making it effectively single-agent,
while a remote one papered over the same gap with a private run_id ->
agent_id side-table populated out of band — the tell that the port was
missing something rather than that being behind a wire was special.

The worker model kept this, and moved the identity one level out: a provider
is attached *as* a provider, with the list of its agents it is here to serve,
and souk claims for all of them on one budget.
"""

from __future__ import annotations

import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from souk.errors import AgentNotFound
from souk_provider_sdk import ProviderIdentity


class TwoAgentProvider:
    """Serves both agents, and answers as whichever one was asked."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def run_stream(self, agent_id: str, run_input: dict):
        self.seen.append(agent_id)
        yield {"type": "RUN_STARTED", "threadId": run_input["threadId"], "runId": run_input["runId"]}
        yield {"type": "TEXT_MESSAGE_START", "messageId": "m1", "role": "assistant"}
        yield {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": f"handled by {agent_id}"}
        yield {"type": "TEXT_MESSAGE_END", "messageId": "m1"}
        yield {"type": "RUN_FINISHED", "threadId": run_input["threadId"], "runId": run_input["runId"]}


async def _register(souk, *names: str):
    """Registers a fresh provider identity, and hands back both halves a
    caller needs afterwards: what souk issued, and the public key that *is*
    this provider (what it attaches and claims as)."""
    identity = ProviderIdentity.generate()
    signature, timestamp = identity.sign_registration(list(names))
    registration = await souk.register_agents(
        identity.public_key, signature, timestamp, [{"name": n} for n in names]
    )
    return registration, identity.public_key


async def test_one_provider_serves_several_agents(souk):
    registration, public_key = await _register(souk, "translator", "summarizer")
    translator = registration.agents["translator"]
    summarizer = registration.agents["summarizer"]

    provider = TwoAgentProvider()
    await souk.attach_provider(public_key, provider, [translator.name, summarizer.name])

    replies = {}
    for agent_id in (translator, summarizer):
        handle = await souk.start_run(agent_id, {"messages": []})
        events = [e async for e in handle.events()]
        replies[agent_id] = next(e["delta"] for e in events if e.get("delta"))

    # Each run reached the provider tagged with the agent it was for, and the
    # provider could answer accordingly.
    assert provider.seen == [translator.name, summarizer.name]
    assert replies[translator] == f"handled by {translator.name}"
    assert replies[summarizer] == f"handled by {summarizer.name}"


async def test_the_run_input_itself_still_carries_no_agent_id(souk):
    """Documents why the parameter exists: AG-UI's schema has no field for
    it, and souk does not smuggle one in — RunAgentInput stays exactly what
    the protocol says it is."""
    registration, public_key = await _register(souk, "solo")
    agent_id = registration.agents["solo"]
    seen: dict = {}

    class Recorder:
        async def run_stream(self, agent_id: str, run_input: dict):
            seen["agent_name"] = agent_id
            seen["keys"] = set(run_input)
            yield {
                "type": "RUN_FINISHED",
                "threadId": run_input["threadId"],
                "runId": run_input["runId"],
            }

    await souk.attach_provider(public_key, Recorder(), [agent_id.name])
    handle = await souk.start_run(agent_id, {"messages": []})
    [_ async for _ in handle.events()]

    assert seen["agent_name"] == agent_id.name
    assert not {"agentId", "agent_id"} & seen["keys"]


async def test_a_provider_cannot_attach_an_agent_it_did_not_register(souk):
    """The check that only became possible once the identity is declared
    up front. Attaching used to derive the provider from the agent, so there
    was nothing to verify it against."""
    mine, mine_key = await _register(souk, "mine")
    theirs, _theirs_key = await _register(souk, "theirs")

    with pytest.raises(AgentNotFound):
        await souk.attach_provider(
            mine_key, TwoAgentProvider(), ["mine", "theirs"]
        )
