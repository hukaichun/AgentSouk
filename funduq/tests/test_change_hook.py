from __future__ import annotations

import ast
import asyncio
import logging
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from funduq.changes import RosterChanged, RunStatusChanged
from funduq.core import Funduq
from funduq_provider_sdk import ProviderIdentity

FUNDUQ_PACKAGE = Path(__file__).resolve().parent.parent / "funduq"


async def _register(funduq: Funduq, name: str = "echo"):
    identity = ProviderIdentity.generate()
    signature, timestamp = identity.sign_registration([name])
    registration = await funduq.register_agents(
        identity.public_key, signature, timestamp, [{"name": name}]
    )
    return registration.agents[name], identity


class _Provider:
    async def run_stream(self, agent: str, run_input: dict):
        yield {"type": "RUN_STARTED", "threadId": run_input.thread_id, "runId": run_input.run_id}
        yield {"type": "RUN_FINISHED", "threadId": run_input.thread_id, "runId": run_input.run_id}


async def _until(predicate, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


async def test_registering_and_attaching_both_change_the_roster(funduq: Funduq, attach) -> None:
    seen: list = []
    funduq.on_change(seen.append)

    agent, identity = await _register(funduq)
    assert seen == [RosterChanged()]

    await attach(identity, _Provider(), [agent.name])
    funduq.detach_all_for(agent.provider_key)

    assert seen == [RosterChanged(), RosterChanged(), RosterChanged()]


async def test_a_run_reports_every_status_it_moves_through(funduq: Funduq, attach) -> None:
    seen: list = []
    funduq.on_change(lambda e: seen.append(e) if isinstance(e, RunStatusChanged) else None)

    agent, _identity = await _register(funduq)
    await attach(_identity, _Provider(), [agent.name])
    handle = await funduq.start_run(agent, {"messages": []})
    [event async for event in handle.events()]
    await _until(lambda: any(e.status == "completed" for e in seen))

    assert [e.status for e in seen] == ["running", "completed"]
    assert {e.run_id for e in seen} == {handle.run_id}


async def test_unsubscribing_stops_it(funduq: Funduq) -> None:
    seen: list = []
    unsubscribe = funduq.on_change(seen.append)

    await _register(funduq, "first")
    unsubscribe()
    await _register(funduq, "second")

    assert seen == [RosterChanged()]


async def test_a_subscriber_that_raises_does_not_break_the_thing_that_notified_it(
    funduq: Funduq, caplog
) -> None:
    caplog.set_level(logging.ERROR, logger="funduq.core")

    def explode(event) -> None:
        raise RuntimeError("subscriber is broken")

    funduq.on_change(explode)
    survivor: list = []
    funduq.on_change(survivor.append)

    agent, _ = await _register(funduq)

    assert agent
    assert survivor == [RosterChanged()]
    assert "on_change subscriber raised" in caplog.text


async def test_events_arrive_before_the_call_that_caused_them_returns(funduq: Funduq) -> None:
    seen: list = []
    funduq.on_change(seen.append)

    await _register(funduq)

    assert seen


def test_nothing_moves_a_run_status_behind_the_hook() -> None:
    offenders = []
    for module in sorted(FUNDUQ_PACKAGE.rglob("*.py")):
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
        "Funduq.mark_run_status, or the change is made without anyone watching "
        "being told — see funduq/changes.py."
    )
