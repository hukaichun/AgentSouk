from __future__ import annotations

import asyncio
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from souk.core import Souk
from souk_provider_sdk import ProviderIdentity
from souk.models import AgentRef, AgentRecord, AgentSummary, RunRecord


async def _register(souk: Souk, name: str = "translator", provider_name: str | None = "Demo"):
    identity = ProviderIdentity.generate()
    signature, timestamp = identity.sign_registration([name])
    registration = await souk.register_agents(
        identity.public_key,
        signature,
        timestamp,
        [{"name": name, "description": "d"}],
        provider_name=provider_name,
    )
    return registration.agents[name]


class _Provider:
    async def run_stream(self, agent_id: str, run_input: dict):
        yield {"type": "RUN_STARTED", "threadId": run_input.thread_id, "runId": run_input.run_id}
        yield {"type": "RUN_FINISHED", "threadId": run_input.thread_id, "runId": run_input.run_id}


async def test_the_roster_is_agent_summaries(souk: Souk) -> None:
    await _register(souk)

    roster = await souk.list_agents()

    assert all(isinstance(a, AgentSummary) for a in roster)
    assert set(roster[0].model_dump()) == {
        "provider_key",
        "name",
        "description",
        "skills",
        "joined_at",
        "last_seen_at",
        "online",
        "provider_name",
    }


async def test_get_agent_is_an_agent_record(souk: Souk) -> None:
    agent_id = await _register(souk)

    agent = await souk.get_agent(agent_id)

    assert isinstance(agent, AgentRecord)
    assert set(agent.model_dump()) == {
        "provider_key",
        "name",
        "agent_card",
        "metadata",
        "joined_at",
        "last_seen_at",
    }


async def test_get_run_is_a_run_record_without_the_storage_columns(souk: Souk, serve) -> None:
    agent_id = (await serve(_Provider(), "echo")).agents["echo"]
    handle = await souk.start_run(agent_id, {"messages": []})
    [event async for event in handle.events()]

    run = await souk.get_run(handle.run_id)

    assert isinstance(run, RunRecord)
    assert set(run.model_dump()) == {
        "run_id",
        "thread_id",
        "provider_key",
        "agent_name",
        "protocol",
        "status",
        "input_json",
        "metadata",
        "created_at",
        "started_at",
        "completed_at",
        "last_activity_at",
    }
    assert run.run_id == handle.run_id
    assert run.thread_id == handle.thread_id
    assert AgentRef(provider_key=run.provider_key, name=run.agent_name) == agent_id
    assert run.protocol == "ag-ui"


async def test_an_unknown_id_is_still_none(souk: Souk) -> None:
    assert await souk.get_agent(AgentRef(provider_key="00" * 32, name="nope")) is None
    assert await souk.get_run("run_nope") is None


async def test_the_models_serialise(souk: Souk) -> None:
    agent_id = await _register(souk)

    dumped = (await souk.list_agents())[0].model_dump(mode="json")

    assert isinstance(dumped["joined_at"], str)
    assert dumped["provider_key"] == agent_id.provider_key
    assert dumped["name"] == agent_id.name
    assert dumped["provider_name"] == "Demo"
