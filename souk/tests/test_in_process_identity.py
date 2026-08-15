"""An in-process provider is not a trusted one.

Sharing a process with souk is not a reason to skip proving who you are, and
it is not a reason for souk to lose track of whether you are actually there.
An earlier version of `attach_provider` was a side door past both: it put an
object in a dictionary, so anything holding the Souk could claim any agent_id,
and the agent stayed invisible to the liveness model that the roster and the
offline fast-fail read — an attached provider showed as offline and calls to
it failed with "agent is currently offline" while it sat right there.

These tests hold the line that in-process and remote go through the same door.
"""

from __future__ import annotations

import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from souk.errors import AgentNotFound, InvalidRegistration
from souk.identity import registration_signing_payload


class Agent:
    async def start(self, run_input: dict):
        return self._events(run_input)

    async def cancel(self, run_id: str) -> None:
        pass

    async def _events(self, run_input: dict):
        yield {"type": "RUN_STARTED", "threadId": run_input["threadId"], "runId": run_input["runId"]}
        yield {"type": "RUN_FINISHED", "threadId": run_input["threadId"], "runId": run_input["runId"]}


def _signed(key: Ed25519PrivateKey, sdk_client_id: str, names: list[str]) -> tuple[str, str, int]:
    timestamp = int(time.time())
    payload = registration_signing_payload(sdk_client_id, names, timestamp)
    return (
        key.public_key().public_bytes_raw().hex(),
        key.sign(payload).hex(),
        timestamp,
    )


async def _register(souk, name: str = "local"):
    key = Ed25519PrivateKey.generate()
    public_key, signature, timestamp = _signed(key, "sdk_local", [name])
    registration = await souk.register_agents(
        "sdk_local", public_key, signature, timestamp, [{"name": name}]
    )
    return registration, registration.agent_ids[name]


async def test_registration_must_prove_it_holds_the_key(souk):
    key = Ed25519PrivateKey.generate()
    public_key, _signature, timestamp = _signed(key, "sdk_1", ["a"])

    with pytest.raises(InvalidRegistration):
        await souk.register_agents("sdk_1", public_key, "00" * 64, timestamp, [{"name": "a"}])


async def test_registration_refuses_a_stale_timestamp(souk):
    """Bounds how long an observed-but-valid signature stays replayable."""
    key = Ed25519PrivateKey.generate()
    stale = int(time.time()) - 3600
    payload = registration_signing_payload("sdk_1", ["a"], stale)

    with pytest.raises(InvalidRegistration):
        await souk.register_agents(
            "sdk_1",
            key.public_key().public_bytes_raw().hex(),
            key.sign(payload).hex(),
            stale,
            [{"name": "a"}],
        )


async def test_attaching_an_unregistered_agent_is_refused(souk):
    """The whole point: being in-process is not a way around registering."""
    with pytest.raises(AgentNotFound):
        await souk.attach_provider("agent_never_registered", Agent())


async def test_an_attached_provider_is_actually_online_and_reachable(souk):
    """The bug this pins: attached-but-offline. souk reported the agent
    offline and fast-failed calls to a provider that was right there."""
    _registration, agent_id = await _register(souk)

    await souk.attach_provider(agent_id, Agent())

    roster = await souk.list_agents()
    assert [a["agent_id"] for a in roster] == [agent_id]
    assert roster[0]["online"] is True

    handle = await souk.start_run(agent_id, {"messages": []})
    assert [e["type"] async for e in handle.events()] == ["RUN_STARTED", "RUN_FINISHED"]


async def test_detaching_marks_it_offline_immediately(souk):
    """A departure souk actually witnessed, unlike a remote provider that
    stops polling and has to be inferred — so it shouldn't have to age out
    of the online window first."""
    _registration, agent_id = await _register(souk)
    await souk.attach_provider(agent_id, Agent())
    assert (await souk.list_agents())[0]["online"] is True

    await souk.detach_provider(agent_id)

    roster = await souk.list_agents()
    assert roster[0]["online"] is False
    # Still listed, just not available — de-listing is a different act.
    assert roster[0]["agent_id"] == agent_id


async def test_registration_issues_a_session_token(souk):
    """The same token a remote provider gets, since it is the same act."""
    registration, _agent_id = await _register(souk)
    assert registration.session_token
    from souk.identity import verify_session_token

    assert verify_session_token(
        registration.session_token, souk.settings.token_signing_secret
    ) == "sdk_local"
