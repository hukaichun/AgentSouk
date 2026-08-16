"""Removing an agent is an explicit, signed act, and only for one nothing
has ever used.

Registering used to double as removing: a batch was the declarative statement
of what an identity offers, so anything missing from it was de-listed on the
spot. That is convenient and it fails silently — a provider that starts with a
partial list removes everything it forgot to mention and logs nothing. The two
acts are separate now, and this file is about the second one.

The guard is nearly the whole feature (what is left of the delete is one row),
so most of this file is refusals.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from souk import repo
from souk.config import CoreSettings
from souk.core import Souk
from souk.errors import AgentInUse, AgentNotFound, InvalidRegistration
from souk.identity import agent_deletion_signing_payload, registration_signing_payload
from souk.models import AgentRef


class _Provider:
    async def run_stream(self, agent_name: str, run_input: dict):
        yield {"type": "RUN_STARTED", "threadId": run_input["threadId"], "runId": run_input["runId"]}
        yield {"type": "RUN_FINISHED", "threadId": run_input["threadId"], "runId": run_input["runId"]}


class _Identity:
    """A provider that can sign for itself, which is what deleting requires."""

    def __init__(self) -> None:
        self.key = Ed25519PrivateKey.generate()
        self.public_key = self.key.public_key().public_bytes_raw().hex()

    async def register(self, souk: Souk, *names: str):
        timestamp = int(time.time())
        return await souk.register_agents(
            self.public_key,
            self.key.sign(registration_signing_payload(list(names), timestamp)).hex(),
            timestamp,
            [{"name": n} for n in names],
        )

    def deletion(self, name: str, timestamp: int | None = None) -> tuple[str, int]:
        timestamp = timestamp if timestamp is not None else int(time.time())
        return (
            self.key.sign(agent_deletion_signing_payload(name, timestamp)).hex(),
            timestamp,
        )

    def registration_signature(self, names: list[str], timestamp: int) -> str:
        return self.key.sign(registration_signing_payload(names, timestamp)).hex()


@pytest.fixture
async def offline_souk(settings: CoreSettings):
    """Deleting requires the agent to be offline, and registering marks it
    seen — so every test here would otherwise have to wait out the window."""
    souk = Souk(settings.model_copy(update={"online_window_seconds": 0}))
    try:
        yield souk
    finally:
        await souk.aclose()


# ---- The signature


async def test_a_registration_signature_is_not_a_deletion_order(offline_souk):
    """The hole this file's domain separation closes, measured before it was.

    A registration signed `"translator:1755300000"` and the obvious deletion
    payload is the same name and the same timestamp — byte for byte. Anyone
    who merely *observed* a provider register one agent held a valid order to
    delete it, for as long as the freshness window allows. No key theft, no
    broken crypto: just the same bytes meaning two things.
    """
    identity = _Identity()
    await identity.register(offline_souk, "translator")
    timestamp = int(time.time())

    replayed = identity.registration_signature(["translator"], timestamp)

    with pytest.raises(InvalidRegistration):
        await offline_souk.delete_agent(
            identity.public_key, "translator", replayed, timestamp
        )
    assert await offline_souk.get_agent(
        AgentRef(provider_key=identity.public_key, name="translator")
    ) is not None


async def test_deleting_needs_the_key_that_registered(offline_souk):
    identity, impostor = _Identity(), _Identity()
    await identity.register(offline_souk, "translator")

    signature, timestamp = impostor.deletion("translator")

    with pytest.raises(InvalidRegistration):
        await offline_souk.delete_agent(
            identity.public_key, "translator", signature, timestamp
        )


async def test_a_stale_deletion_is_refused(offline_souk):
    """Same bound as registration: a captured signature stops working."""
    identity = _Identity()
    await identity.register(offline_souk, "translator")

    signature, timestamp = identity.deletion("translator", timestamp=int(time.time()) - 3600)

    with pytest.raises(InvalidRegistration):
        await offline_souk.delete_agent(
            identity.public_key, "translator", signature, timestamp
        )


# ---- The guard


async def test_an_unused_agent_is_deleted_and_its_name_is_free_again(offline_souk):
    """What deleting is actually for: a registration that never became
    anything — a typo, a test, a batch from the wrong config."""
    identity = _Identity()
    await identity.register(offline_souk, "typo")
    agent = AgentRef(provider_key=identity.public_key, name="typo")

    signature, timestamp = identity.deletion("typo")
    await offline_souk.delete_agent(identity.public_key, "typo", signature, timestamp)

    assert await offline_souk.get_agent(agent) is None
    # And nothing stops it being offered again.
    await identity.register(offline_souk, "typo")
    assert await offline_souk.get_agent(agent) is not None


async def test_deleting_an_agent_that_never_existed_is_not_found(offline_souk):
    identity = _Identity()
    await identity.register(offline_souk, "real")

    signature, timestamp = identity.deletion("imaginary")

    with pytest.raises(AgentNotFound):
        await offline_souk.delete_agent(
            identity.public_key, "imaginary", signature, timestamp
        )


async def test_an_online_agent_is_refused(settings: CoreSettings):
    """A provider still checking in is still serving it."""
    souk = Souk(settings)
    try:
        identity = _Identity()
        await identity.register(souk, "busy")  # registering marks it seen

        signature, timestamp = identity.deletion("busy")
        with pytest.raises(AgentInUse) as refused:
            await souk.delete_agent(identity.public_key, "busy", signature, timestamp)
        assert refused.value.reason == "online"
    finally:
        await souk.aclose()


async def test_an_attached_agent_is_refused(offline_souk):
    """A wedged worker can be offline *and* attached, so both are asked."""
    identity = _Identity()
    await identity.register(offline_souk, "local")
    await offline_souk.attach_provider(identity.public_key, _Provider(), ["local"])

    signature, timestamp = identity.deletion("local")
    with pytest.raises(AgentInUse) as refused:
        await offline_souk.delete_agent(identity.public_key, "local", signature, timestamp)
    assert refused.value.reason == "attached"


async def test_an_agent_with_a_paused_run_is_refused(offline_souk):
    """`input-required` is the status a plausible shorter liveness check
    misses: that run is waiting on a human who is coming back, and deleting
    it would destroy something nobody has finished with."""
    identity = _Identity()
    registered = await identity.register(offline_souk, "paused")
    agent = registered.agents["paused"]

    async with offline_souk.session() as session:
        thread_id = await repo.ensure_thread(session, agent, None)
        created = await repo.create_run(session, thread_id, agent, "ag-ui", {})
        await repo.mark_run_status(session, created["run_id"], "input-required")
        await session.commit()

    signature, timestamp = identity.deletion("paused")
    with pytest.raises(AgentInUse) as refused:
        await offline_souk.delete_agent(identity.public_key, "paused", signature, timestamp)
    assert refused.value.reason == "active_run"


async def test_an_agent_that_has_held_a_conversation_is_refused(offline_souk):
    """Built by running something rather than by inserting a row: a test that
    creates the exact state the check reads would pass even if the check were
    wired to the wrong column.

    This is the refusal that makes `threads`' foreign key a rule rather than
    an obstacle — a thread must name an agent, so an agent with threads
    cannot go. It is also what keeps deleting from ever reaching a caller's
    own messages, which souk stores deliberately.
    """
    identity = _Identity()
    registered = (await identity.register(offline_souk, "worked")).agents
    await offline_souk.attach_provider(identity.public_key, _Provider(), ["worked"])

    handle = await offline_souk.start_run(registered["worked"], {"messages": []})
    assert [e["type"] async for e in handle.events()][-1] == "RUN_FINISHED"
    await offline_souk.detach_provider(identity.public_key)

    signature, timestamp = identity.deletion("worked")
    with pytest.raises(AgentInUse) as refused:
        await offline_souk.delete_agent(identity.public_key, "worked", signature, timestamp)
    assert refused.value.reason == "has_history"

    # Retiring it is the act that does work here, and it needs no deletion:
    # stop offering it, and it goes offline with its record intact.
    await identity.register(offline_souk, "something-else")
    assert [a.online for a in await offline_souk.list_agents() if a.name == "worked"] == [False]
    assert await offline_souk.get_agent(registered["worked"]) is not None
