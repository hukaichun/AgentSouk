"""What the query methods hand back, as a contract rather than as whatever
`repo.py` happened to build.

souk's field names were only ever written down in row-building code, so
everyone downstream learned them by reading it — the gateway carried a
hand-written model listing exactly these keys, kept in step by nobody. These
assertions are the other half of moving that model to where the data is
produced: the field *set* is pinned, so a rename is a deliberate act with a
failing test attached rather than something a consumer discovers.

Whole field sets, not spot checks. A spot check passes while a field quietly
disappears.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from souk.core import Souk
from souk.identity import registration_signing_payload
from souk.models import AgentRecord, AgentSummary, RunRecord


async def _register(souk: Souk, name: str = "translator", provider_name: str | None = "Demo"):
    key = Ed25519PrivateKey.generate()
    public_key = key.public_key().public_bytes_raw().hex()
    timestamp = int(time.time())
    registration = await souk.register_agents(
        public_key,
        key.sign(registration_signing_payload([name], timestamp)).hex(),
        timestamp,
        [{"name": name, "description": "d"}],
        provider_name=provider_name,
    )
    return registration.agent_ids[name], public_key


class _Provider:
    async def run_stream(self, agent_id: str, run_input: dict):
        yield {"type": "RUN_STARTED", "threadId": run_input["threadId"], "runId": run_input["runId"]}
        yield {"type": "RUN_FINISHED", "threadId": run_input["threadId"], "runId": run_input["runId"]}


async def test_the_roster_is_agent_summaries(souk: Souk) -> None:
    await _register(souk)

    roster = await souk.list_agents()

    assert all(isinstance(a, AgentSummary) for a in roster)
    assert set(roster[0].model_dump()) == {
        "agent_id",
        "name",
        "description",
        "skills",
        "joined_at",
        "last_seen_at",
        "online",
        "public_key",
        "provider_name",
    }


async def test_get_agent_is_an_agent_record(souk: Souk) -> None:
    agent_id, _ = await _register(souk)

    agent = await souk.get_agent(agent_id)

    assert isinstance(agent, AgentRecord)
    assert set(agent.model_dump()) == {
        "agent_id",
        "name",
        "agent_card",
        "metadata",
        "joined_at",
        "last_seen_at",
    }


async def test_get_run_is_a_run_record_without_the_storage_columns(souk: Souk) -> None:
    """Runs live in `thread_history` next to messages, and `get_run` used to
    `select(thread_history)` — so it also returned `id`, `kind`, `message_id`
    and `message_json`, which say where souk keeps a run rather than anything
    about the run. Nothing read them; a caller that started to would be
    depending on the storage layout.
    """
    agent_id, public_key = await _register(souk)
    await souk.attach_provider(public_key, _Provider(), [agent_id])
    handle = await souk.start_run(agent_id, {"messages": []})
    [event async for event in handle.events()]

    run = await souk.get_run(handle.run_id)

    assert isinstance(run, RunRecord)
    assert set(run.model_dump()) == {
        "run_id",
        "thread_id",
        "agent_id",
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
    assert run.agent_id == agent_id
    assert run.protocol == "ag-ui"


async def test_an_unknown_id_is_still_none(souk: Souk) -> None:
    """Typing the hit does not change what a miss looks like."""
    assert await souk.get_agent("agent_nope") is None
    assert await souk.get_run("run_nope") is None


async def test_the_models_serialise(souk: Souk) -> None:
    """The gateway turns these into JSON, so `mode="json"` has to work on
    every field — the datetimes are the ones that would bite."""
    agent_id, _ = await _register(souk)

    dumped = (await souk.list_agents())[0].model_dump(mode="json")

    assert isinstance(dumped["joined_at"], str)
    assert dumped["agent_id"] == agent_id
    assert dumped["provider_name"] == "Demo"
