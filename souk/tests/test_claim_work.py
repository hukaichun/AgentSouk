"""Claiming work is a domain act, not a transport one.

Every worker comes through here — the one souk runs in its own process for
an attached agent as much as one behind a wire — because deciding whether
an identity may run these agents is a domain question, not a framing one. A
second transport should implement framing and call this, never re-derive who
owns what: the filtering below is the only thing stopping a valid token for
one provider being used to claim another's agents.

Claiming is also the hand-over. A claimed run comes back with its input and
is already recorded as taken, in one step — there is no second call in which
the worker announces what it took, and so no window in between for anything
to observe a run that has been handed out but belongs to nobody.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from souk import repo
from souk.errors import InvalidRegistration
from souk.identity import registration_signing_payload


async def _register(souk, *names: str):
    """Registers a fresh provider identity, and hands back both halves a
    caller needs afterwards: what souk issued, and the public key that *is*
    this provider (what it attaches and claims as)."""
    key = Ed25519PrivateKey.generate()
    public_key = key.public_key().public_bytes_raw().hex()
    timestamp = int(time.time())
    registration = await souk.register_agents(
        public_key,
        key.sign(registration_signing_payload(list(names), timestamp)).hex(),
        timestamp,
        [{"name": n} for n in names],
    )
    return registration, public_key


async def _enqueue(souk, agent, run_input=None):
    """A run that exists in the database, not only in the broker.

    These tests used to dispatch fabricated ids (`souk.enqueue_run("run_1",
    …, "thread_1", …)`), which worked because a status write against a
    non-existent run updated zero rows and said nothing. It says something now
    (repo.mark_run_status), so a test that wants a run has to make one — which
    is the same thing the change is for: souk's dispatch state and the
    database are not allowed to disagree quietly, in tests either.
    """
    run_input = run_input if run_input is not None else {}
    async with souk.session() as session:
        thread_id = await repo.ensure_thread(session, agent, None)
        created = await repo.create_run(session, thread_id, agent, "ag-ui", run_input)
        await session.commit()
    souk.enqueue_run(created["run_id"], agent, thread_id, run_input, "ag-ui")
    return created["run_id"], thread_id


async def test_claiming_returns_queued_runs_with_their_input(souk):
    registration, public_key = await _register(souk, "a")
    agent = registration.agents["a"]
    run_id, thread_id = await _enqueue(souk, agent, {"messages": [{"role": "user"}]})

    runs = await souk.claim_work(registration.session_token, [agent.name])

    assert [(r.run_id, r.agent, r.thread_id) for r in runs] == [(run_id, agent, thread_id)]
    # With the input, not just a pointer to it — a worker leaves this call
    # able to start, rather than waiting to be told what to run.
    assert runs[0].run_input == {"messages": [{"role": "user"}]}


async def test_a_claimed_run_is_recorded_as_taken(souk):
    """What makes a later cancel unambiguous: souk knows somebody has it,
    so it asks and waits rather than recording an outcome itself (see
    handlers._handle_cancel's two cases)."""
    registration, public_key = await _register(souk, "a")
    agent = registration.agents["a"]
    run_id, _thread_id = await _enqueue(souk, agent)
    assert souk.broker.get(run_id).claimed_by is None

    await souk.claim_work(registration.session_token, [agent.name])

    assert souk.broker.get(run_id).claimed_by == public_key


async def test_souk_asks_the_worker_that_claimed_the_run_to_stop(souk):
    """Cancellation reaches a worker through what it supplied when it
    claimed — souk holds no provider object to call into."""
    registration, public_key = await _register(souk, "a")
    agent = registration.agents["a"]
    run_id, _thread_id = await _enqueue(souk, agent)
    asked: list[str] = []

    await souk.claim_work(registration.session_token, [agent.name], on_cancel=asked.append)
    souk.cancel_run(run_id)

    async with asyncio.timeout(1):
        while not asked:
            await asyncio.sleep(0)
    assert asked == [run_id]
    # Asked, not decided: souk is still dispatching the run, waiting to see
    # what its stream does, rather than having ended it here.
    assert souk.broker.get(run_id) is not None


async def test_a_token_cannot_claim_another_providers_agents(souk):
    """The check that matters. Both are validly registered; the token for one
    must not reach the other's work."""
    mine, _mine_key = await _register(souk, "mine")
    theirs, _theirs_key = await _register(souk, "theirs")
    their_agent = theirs.agents["theirs"]
    their_run, _thread_id = await _enqueue(souk, their_agent)

    runs = await souk.claim_work(mine.session_token, [their_agent.name])

    assert runs == []
    # And it is still there for its rightful owner.
    assert [r.run_id for r in await souk.claim_work(theirs.session_token, [their_agent.name])] == [
        their_run
    ]


async def test_an_invalid_token_is_refused(souk):
    with pytest.raises(InvalidRegistration):
        await souk.claim_work("not-a-real-token", ["agent_whatever"])


async def test_claiming_marks_the_agent_as_seen(souk):
    """How a remote provider stays online at all — the mirror of the
    heartbeat that keeps an attached one visible."""
    registration, public_key = await _register(souk, "a")
    agent = registration.agents["a"]

    await souk.claim_work(registration.session_token, [agent.name])

    assert (await souk.list_agents())[0].online is True


async def test_max_claim_limits_and_zero_claims_nothing(souk):
    registration, public_key = await _register(souk, "a")
    agent = registration.agents["a"]
    for _ in range(3):
        await _enqueue(souk, agent)

    # 0 is explicitly "no capacity", not "unlimited" — and must not be
    # confused with None, which is what unlimited means.
    assert await souk.claim_work(registration.session_token, [agent.name], max_claim=0) == []

    claimed = await souk.claim_work(registration.session_token, [agent.name], max_claim=2)
    assert len(claimed) == 2
    assert len(await souk.claim_work(registration.session_token, [agent.name])) == 1


async def test_long_polling_returns_as_soon_as_work_arrives(souk):
    registration, public_key = await _register(souk, "a")
    agent = registration.agents["a"]

    late: dict[str, str] = {}

    async def enqueue_soon():
        await asyncio.sleep(0.05)
        late["run_id"], _ = await _enqueue(souk, agent)

    task = asyncio.create_task(enqueue_soon())
    started = time.monotonic()
    runs = await souk.claim_work(registration.session_token, [agent.name], wait_seconds=5)
    elapsed = time.monotonic() - started
    await task

    assert [r.run_id for r in runs] == [late["run_id"]]
    assert elapsed < 2  # woke on the work, didn't sit out the full wait


async def test_long_polling_gives_up_when_nothing_arrives(souk):
    registration, public_key = await _register(souk, "a")

    runs = await souk.claim_work(
        registration.session_token, [registration.agents["a"]], wait_seconds=0.1
    )

    assert runs == []
