from __future__ import annotations

import asyncio
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from souk import repo
from souk.config import CoreSettings
from souk.core import Souk
from souk.errors import AgentInUse, AgentNotFound, InvalidRegistration
from souk_provider_sdk import ProviderIdentity
from souk.models import AgentRef


class _Provider:
    async def run_stream(self, agent_name: str, run_input: dict):
        yield {"type": "RUN_STARTED", "threadId": run_input.thread_id, "runId": run_input.run_id}
        yield {"type": "RUN_FINISHED", "threadId": run_input.thread_id, "runId": run_input.run_id}


class _Identity(ProviderIdentity):

    def __init__(self) -> None:
        super().__init__(Ed25519PrivateKey.generate())

    async def register(self, souk: Souk, *names: str):
        signature, timestamp = self.sign_registration(list(names))
        return await souk.register_agents(
            self.public_key, signature, timestamp, [{"name": n} for n in names]
        )

    def deletion(self, name: str, timestamp: int | None = None) -> tuple[str, int]:
        return self.sign_deletion(name, timestamp)

    def registration_signature(self, names: list[str], timestamp: int) -> str:
        signature, _ = self.sign_registration(names, timestamp)
        return signature


async def test_a_registration_signature_is_not_a_deletion_order(souk):
    identity = _Identity()
    await identity.register(souk, "translator")
    timestamp = int(time.time())

    replayed = identity.registration_signature(["translator"], timestamp)

    with pytest.raises(InvalidRegistration):
        await souk.delete_agent(
            identity.public_key, "translator", replayed, timestamp
        )
    assert await souk.get_agent(
        AgentRef(provider_key=identity.public_key, name="translator")
    ) is not None


async def test_deleting_needs_the_key_that_registered(souk):
    identity, impostor = _Identity(), _Identity()
    await identity.register(souk, "translator")

    signature, timestamp = impostor.deletion("translator")

    with pytest.raises(InvalidRegistration):
        await souk.delete_agent(
            identity.public_key, "translator", signature, timestamp
        )


async def test_a_stale_deletion_is_refused(souk):
    identity = _Identity()
    await identity.register(souk, "translator")

    signature, timestamp = identity.deletion("translator", timestamp=int(time.time()) - 3600)

    with pytest.raises(InvalidRegistration):
        await souk.delete_agent(
            identity.public_key, "translator", signature, timestamp
        )


async def test_an_unused_agent_is_deleted_and_its_name_is_free_again(souk):
    identity = _Identity()
    await identity.register(souk, "typo")
    agent = AgentRef(provider_key=identity.public_key, name="typo")

    signature, timestamp = identity.deletion("typo")
    await souk.delete_agent(identity.public_key, "typo", signature, timestamp)

    assert await souk.get_agent(agent) is None
    await identity.register(souk, "typo")
    assert await souk.get_agent(agent) is not None


async def test_deleting_an_agent_that_never_existed_is_not_found(souk):
    identity = _Identity()
    await identity.register(souk, "real")

    signature, timestamp = identity.deletion("imaginary")

    with pytest.raises(AgentNotFound):
        await souk.delete_agent(
            identity.public_key, "imaginary", signature, timestamp
        )


async def test_an_agent_someone_is_serving_is_refused(souk, attach):
    identity = _Identity()
    await identity.register(souk, "local")
    await attach(identity, _Provider(), ["local"])

    signature, timestamp = identity.deletion("local")
    with pytest.raises(AgentInUse) as refused:
        await souk.delete_agent(identity.public_key, "local", signature, timestamp)
    assert refused.value.reason == "connected"


async def test_an_agent_with_a_paused_run_is_refused(souk):
    identity = _Identity()
    registered = await identity.register(souk, "paused")
    agent = registered.agents["paused"]

    async with souk.session() as session:
        thread_id = await repo.ensure_thread(session, agent, None)
        created = await repo.create_run(session, thread_id, agent, "ag-ui", {})
        await repo.mark_run_status(session, created["run_id"], "input-required")
        await session.commit()

    signature, timestamp = identity.deletion("paused")
    with pytest.raises(AgentInUse) as refused:
        await souk.delete_agent(identity.public_key, "paused", signature, timestamp)
    assert refused.value.reason == "active_run"


async def test_an_agent_that_has_held_a_conversation_is_refused(souk, attach):
    identity = _Identity()
    registered = (await identity.register(souk, "worked")).agents
    await attach(identity, _Provider(), ["worked"])

    handle = await souk.start_run(registered["worked"], {"messages": []})
    assert [e["type"] async for e in handle.events()][-1] == "RUN_FINISHED"
    await souk.detach_provider(identity.public_key)

    signature, timestamp = identity.deletion("worked")
    with pytest.raises(AgentInUse) as refused:
        await souk.delete_agent(identity.public_key, "worked", signature, timestamp)
    assert refused.value.reason == "has_history"

    await identity.register(souk, "something-else")
    assert [a.online for a in await souk.list_agents() if a.name == "worked"] == [False]
    assert await souk.get_agent(registered["worked"]) is not None
