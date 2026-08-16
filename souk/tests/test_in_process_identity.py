
from __future__ import annotations

import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from souk.errors import AgentNotFound, InvalidRegistration
from souk.identity import registration_signing_payload
from souk_provider_sdk import ProviderIdentity


class LocalProvider:
    async def run_stream(self, agent_id: str, run_input: dict):
        yield {"type": "RUN_STARTED", "threadId": run_input["threadId"], "runId": run_input["runId"]}
        yield {"type": "RUN_FINISHED", "threadId": run_input["threadId"], "runId": run_input["runId"]}


def _signed(identity: ProviderIdentity, names: list[str]) -> tuple[str, str, int]:
    signature, timestamp = identity.sign_registration(names)
    return identity.public_key, signature, timestamp


async def _register(souk, name: str = "local"):
    identity = ProviderIdentity.generate()
    public_key, signature, timestamp = _signed(identity, [name])
    registration = await souk.register_agents(
        public_key, signature, timestamp, [{"name": name}]
    )
    return registration, identity, registration.agents[name]


async def test_registration_must_prove_it_holds_the_key(souk):
    identity = ProviderIdentity.generate()
    public_key, _signature, timestamp = _signed(identity, ["a"])

    with pytest.raises(InvalidRegistration):
        await souk.register_agents(public_key, "00" * 64, timestamp, [{"name": "a"}])


async def test_registration_refuses_a_stale_timestamp(souk):
    key = Ed25519PrivateKey.generate()
    stale = int(time.time()) - 3600
    payload = registration_signing_payload(["a"], stale)

    with pytest.raises(InvalidRegistration):
        await souk.register_agents(
            key.public_key().public_bytes_raw().hex(),
            key.sign(payload).hex(),
            stale,
            [{"name": "a"}],
        )


async def test_attaching_an_unregistered_agent_is_refused(souk, attach):
    """Sharing souk's process is not a reason to be trusted, and not a reason
    to take a different path: the names have to be ones this key registered."""
    with pytest.raises(AgentNotFound):
        await attach(ProviderIdentity.generate(), LocalProvider(), ["agent_never_registered"])


async def test_an_attached_provider_is_actually_online_and_reachable(souk, attach):
    _registration, identity, agent_id = await _register(souk)

    await attach(identity, LocalProvider(), [agent_id.name])

    roster = await souk.list_agents()
    assert [a.name for a in roster] == [agent_id.name]
    assert roster[0].online is True

    handle = await souk.start_run(agent_id, {"messages": []})
    assert [e["type"] async for e in handle.events()] == ["RUN_STARTED", "RUN_FINISHED"]


async def test_detaching_marks_it_offline_immediately(souk, attach):
    """A departure souk witnessed, so it takes effect at once rather than
    being inferred from silence later."""
    _registration, identity, agent_id = await _register(souk)
    await attach(identity, LocalProvider(), [agent_id.name])
    assert (await souk.list_agents())[0].online is True

    await souk.detach_provider(identity.public_key)

    roster = await souk.list_agents()
    assert roster[0].online is False
    assert roster[0].name == agent_id.name
