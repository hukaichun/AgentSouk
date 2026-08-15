"""Claiming work is a domain act, not a gRPC one.

`attach_provider` and `claim_work` are the two ways a provider takes on
runs — in-process by being present, remote by coming to ask. Both decide the
same thing (may this identity run these agents?), so both belong in core. A
second transport should implement framing and call this, never re-derive who
owns what: the filtering below is the only thing stopping a valid token for
one provider being used to poll for another's agents.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from souk.errors import InvalidRegistration
from souk.identity import registration_signing_payload


async def _register(souk, sdk_client_id: str, *names: str):
    key = Ed25519PrivateKey.generate()
    timestamp = int(time.time())
    return await souk.register_agents(
        sdk_client_id,
        key.public_key().public_bytes_raw().hex(),
        key.sign(registration_signing_payload(sdk_client_id, list(names), timestamp)).hex(),
        timestamp,
        [{"name": n} for n in names],
    )


async def test_claiming_returns_queued_runs(souk):
    registration = await _register(souk, "sdk_1", "a")
    agent_id = registration.agent_ids["a"]
    souk.enqueue_run("run_1", agent_id, "thread_1", {}, "ag-ui")

    runs = await souk.claim_work(registration.session_token, [agent_id])

    assert [r.run_id for r in runs] == ["run_1"]


async def test_a_token_cannot_claim_another_providers_agents(souk):
    """The check that matters. Both are validly registered; the token for one
    must not reach the other's work."""
    mine = await _register(souk, "sdk_mine", "mine")
    theirs = await _register(souk, "sdk_theirs", "theirs")
    their_agent = theirs.agent_ids["theirs"]
    souk.enqueue_run("run_theirs", their_agent, "thread_1", {}, "ag-ui")

    runs = await souk.claim_work(mine.session_token, [their_agent])

    assert runs == []
    # And it is still there for its rightful owner.
    assert [r.run_id for r in await souk.claim_work(theirs.session_token, [their_agent])] == [
        "run_theirs"
    ]


async def test_an_invalid_token_is_refused(souk):
    with pytest.raises(InvalidRegistration):
        await souk.claim_work("not-a-real-token", ["agent_whatever"])


async def test_claiming_marks_the_agent_as_seen(souk):
    """How a remote provider stays online at all — the mirror of the
    heartbeat that keeps an attached one visible."""
    registration = await _register(souk, "sdk_1", "a")
    agent_id = registration.agent_ids["a"]

    await souk.claim_work(registration.session_token, [agent_id])

    assert (await souk.list_agents())[0]["online"] is True


async def test_max_claim_limits_and_zero_claims_nothing(souk):
    registration = await _register(souk, "sdk_1", "a")
    agent_id = registration.agent_ids["a"]
    for i in range(3):
        souk.enqueue_run(f"run_{i}", agent_id, "thread_1", {}, "ag-ui")

    # 0 is explicitly "no capacity", not "unlimited" — and must not be
    # confused with None, which is what unlimited means.
    assert await souk.claim_work(registration.session_token, [agent_id], max_claim=0) == []

    claimed = await souk.claim_work(registration.session_token, [agent_id], max_claim=2)
    assert len(claimed) == 2
    assert len(await souk.claim_work(registration.session_token, [agent_id])) == 1


async def test_long_polling_returns_as_soon_as_work_arrives(souk):
    registration = await _register(souk, "sdk_1", "a")
    agent_id = registration.agent_ids["a"]

    async def enqueue_soon():
        await asyncio.sleep(0.05)
        souk.enqueue_run("run_late", agent_id, "thread_1", {}, "ag-ui")

    task = asyncio.create_task(enqueue_soon())
    started = time.monotonic()
    runs = await souk.claim_work(registration.session_token, [agent_id], wait_seconds=5)
    elapsed = time.monotonic() - started
    await task

    assert [r.run_id for r in runs] == ["run_late"]
    assert elapsed < 2  # woke on the work, didn't sit out the full wait


async def test_long_polling_gives_up_when_nothing_arrives(souk):
    registration = await _register(souk, "sdk_1", "a")

    runs = await souk.claim_work(
        registration.session_token, [registration.agent_ids["a"]], wait_seconds=0.1
    )

    assert runs == []
