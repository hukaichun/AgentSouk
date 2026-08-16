"""`Souk.on_change`: being told, instead of asking again.

An adapter that has to tell its own clients "the toolbox changed" was left
polling `list_agents()` on a timer, for facts souk already computes. These
cover what is promised — coarse events, synchronous delivery, no history —
and, just as importantly, what is not.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from souk.changes import RosterChanged, RunStatusChanged
from souk.core import Souk
from souk.identity import registration_signing_payload

SOUK_PACKAGE = Path(__file__).resolve().parent.parent / "souk"


async def _register(souk: Souk, name: str = "echo"):
    key = Ed25519PrivateKey.generate()
    public_key = key.public_key().public_bytes_raw().hex()
    timestamp = int(time.time())
    registration = await souk.register_agents(
        public_key,
        key.sign(registration_signing_payload([name], timestamp)).hex(),
        timestamp,
        [{"name": name}],
    )
    return registration.agents[name]


class _Provider:
    async def run_stream(self, agent: str, run_input: dict):
        yield {"type": "RUN_STARTED", "threadId": run_input["threadId"], "runId": run_input["runId"]}
        yield {"type": "RUN_FINISHED", "threadId": run_input["threadId"], "runId": run_input["runId"]}


async def _until(predicate, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


async def test_registering_and_attaching_both_change_the_roster(souk: Souk) -> None:
    """Attaching adds no agent, but it changes what the roster *answers* —
    `AgentSummary.online` is part of that answer."""
    seen: list = []
    souk.on_change(seen.append)

    agent = await _register(souk)
    assert seen == [RosterChanged()]

    await souk.attach_provider(agent.provider_key, _Provider(), [agent.name])
    await souk.detach_provider(agent.provider_key)

    assert seen == [RosterChanged(), RosterChanged(), RosterChanged()]


async def test_a_run_reports_every_status_it_moves_through(souk: Souk) -> None:
    seen: list = []
    souk.on_change(lambda e: seen.append(e) if isinstance(e, RunStatusChanged) else None)

    agent = await _register(souk)
    await souk.attach_provider(agent.provider_key, _Provider(), [agent.name])
    handle = await souk.start_run(agent, {"messages": []})
    [event async for event in handle.events()]
    await _until(lambda: any(e.status == "completed" for e in seen))

    # 'queued' is not here on purpose: a run is created queued, it does not
    # transition into it, so there is nothing to announce before a subscriber
    # could have learned of the run at all.
    assert [e.status for e in seen] == ["running", "completed"]
    assert {e.run_id for e in seen} == {handle.run_id}


async def test_unsubscribing_stops_it(souk: Souk) -> None:
    seen: list = []
    unsubscribe = souk.on_change(seen.append)

    await _register(souk, "first")
    unsubscribe()
    await _register(souk, "second")

    assert seen == [RosterChanged()]


async def test_a_subscriber_that_raises_does_not_break_the_thing_that_notified_it(
    souk: Souk, caplog
) -> None:
    """The contract souk can actually keep: your callback is your problem.
    A registration must not fail because something watching it threw."""
    caplog.set_level(logging.ERROR, logger="souk.core")

    def explode(event) -> None:
        raise RuntimeError("subscriber is broken")

    souk.on_change(explode)
    survivor: list = []
    souk.on_change(survivor.append)

    agent = await _register(souk)

    assert agent
    assert survivor == [RosterChanged()]
    assert "on_change subscriber raised" in caplog.text


async def test_events_arrive_before_the_call_that_caused_them_returns(souk: Souk) -> None:
    """Synchronous, not queued — the same contract as `claim_work`'s
    `on_cancel`. A caller that subscribes and then registers has the event in
    hand on the next line, with no sleep and no polling."""
    seen: list = []
    souk.on_change(seen.append)

    await _register(souk)

    assert seen  # no await, no sleep, no _until


def test_nothing_moves_a_run_status_behind_the_hook() -> None:
    """The reason `Souk.mark_run_status` exists at all.

    Seven places move a run today, and the eighth — added later, by someone
    who did not know about this file — is the one that silently does not
    notify. So the rule is enforced rather than remembered: `repo` is the
    storage half, and only `Souk` may call it.
    """
    offenders = []
    for module in sorted(SOUK_PACKAGE.rglob("*.py")):
        if module.name in ("repo.py", "core.py"):
            continue
        for node in ast.walk(ast.parse(module.read_text())):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "mark_run_status"
                and isinstance(node.value, ast.Name)
                and node.value.id == "repo"
            ):
                offenders.append(f"{module.name}:{node.lineno}")

    assert not offenders, (
        f"these call repo.mark_run_status directly: {offenders}. Use "
        "Souk.mark_run_status, or the change is made without anyone watching "
        "being told — see souk/changes.py."
    )
