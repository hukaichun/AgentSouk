"""Covers the AG-UI HTTP surface: the optional POST /threads endpoint,
and that /agui/... runs mint a fresh thread automatically for an
unrecognized threadId rather than requiring POST /threads first (see
souk-no-forced-protocol-deviation) — real ag_ui.core.RunAgentInput shape
and all.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from souk import repo
from souk.schema import agents


async def _register(client, identity, name, **extra):
    body = identity.register_body([{"name": name, **extra}])
    resp = await client.post("/agents/register", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["agent_ids"][name]


def _run_input(thread_id: str, message: str = "hi") -> dict:
    """The real ag_ui.core.RunAgentInput wire shape — threadId/runId/
    state/messages/tools/context/forwardedProps all required by the real
    schema (see souk/models.py's module docstring for why souk no longer
    has its own looser version of this). runId is required by the schema
    but never actually used by souk — any placeholder satisfies it.
    """
    return {
        "threadId": thread_id,
        "runId": "ignored",
        "state": None,
        "messages": [{"id": "whatever", "role": "user", "content": message}],
        "tools": [],
        "context": [],
        "forwardedProps": None,
    }


def _run_started_thread_id(sse_body: str) -> str:
    """Parses the resolved thread_id out of the stream's own RUN_STARTED
    event — the standard, in-band place a client learns it (no custom
    X-Souk-Thread-Id header anymore — see souk-no-forced-protocol-deviation).
    """
    for line in sse_body.splitlines():
        if line.startswith("data: "):
            event = json.loads(line[len("data: ") :])
            if event.get("type") == "RUN_STARTED":
                return event["threadId"]
    raise AssertionError(f"no RUN_STARTED event found in: {sse_body!r}")


def _run_started_run_id(sse_body: str) -> str:
    for line in sse_body.splitlines():
        if line.startswith("data: "):
            event = json.loads(line[len("data: ") :])
            if event.get("type") == "RUN_STARTED":
                return event["runId"]
    raise AssertionError(f"no RUN_STARTED event found in: {sse_body!r}")


async def test_create_thread_by_name_and_by_id_return_real_thread_ids(client, new_identity):
    identity = new_identity()
    agent_id = await _register(client, identity, "greeter")

    by_name = await client.post("/threads/greeter")
    assert by_name.status_code == 200, by_name.text
    assert by_name.json()["thread_id"].startswith("thread_")

    by_id = await client.post(f"/threads/id/{agent_id}")
    assert by_id.status_code == 200, by_id.text
    assert by_id.json()["thread_id"] != by_name.json()["thread_id"]


async def test_create_thread_for_an_unregistered_agent_404s(client):
    resp = await client.post("/threads/id/agent_does_not_exist")
    assert resp.status_code == 404


async def test_agui_run_mints_a_fresh_thread_for_an_unrecognized_thread_id(client, new_identity, session):
    """AG-UI's `threadId` is caller-minted and required by the schema —
    an id souk has never seen is a brand new conversation, not an error
    (unlike A2A's optional `contextId` — see test_api_a2a.py). Agent is
    marked offline purely so the run resolves immediately instead of
    streaming forever waiting for a provider that will never claim it —
    unrelated to what this test actually checks (the minted thread_id).
    """
    identity = new_identity()
    agent_id = await _register(client, identity, "greeter")
    await session.execute(
        update(agents)
        .where(agents.c.agent_id == agent_id)
        .values(last_seen_at=datetime.now(timezone.utc) - timedelta(seconds=120))
    )
    await session.commit()

    resp = await client.post("/agui/greeter", json=_run_input("thread_made_up"))
    assert resp.status_code == 200, resp.text
    real_thread_id = _run_started_thread_id(resp.text)
    assert real_thread_id.startswith("thread_")
    assert real_thread_id != "thread_made_up"


async def test_agui_run_against_an_offline_agent_fails_fast(client, new_identity, session):
    identity = new_identity()
    agent_id = await _register(client, identity, "translator")

    await session.execute(
        update(agents)
        .where(agents.c.agent_id == agent_id)
        .values(last_seen_at=datetime.now(timezone.utc) - timedelta(seconds=120))
    )
    await session.commit()

    created = await client.post("/threads/translator")
    thread_id = created.json()["thread_id"]

    resp = await client.post("/agui/translator", json=_run_input(thread_id))
    assert resp.status_code == 200
    body_text = resp.text
    assert _run_started_thread_id(body_text) == thread_id
    assert "RUN_ERROR" in body_text
    assert "offline" in body_text


async def _offline_run_with_metadata(client, session, name, agent_id, metadata):
    """Agent marked offline purely for the same reason as
    test_agui_run_against_an_offline_agent_fails_fast: the fast-fail path
    resolves synchronously (still going through ensure_thread/create_run
    with the real metadata first — see api_agui._run_agent), so it's
    enough to check what got persisted without needing a live provider.
    """
    await session.execute(
        update(agents)
        .where(agents.c.agent_id == agent_id)
        .values(last_seen_at=datetime.now(timezone.utc) - timedelta(seconds=120))
    )
    await session.commit()
    body = _run_input("thread_made_up")
    body["metadata"] = metadata
    return await client.post(f"/agui/{name}", json=body)


async def test_agui_run_with_valid_actor_chain_stores_verified_chain(client, session, new_identity):
    caller, agent_id_registrant = new_identity(), new_identity()
    agent_id = await _register(client, agent_id_registrant, "greeter")
    subject = {"type": "user", "id": "employee_x"}
    chain = [caller.sign_chain_hop(subject)]

    resp = await _offline_run_with_metadata(
        client, session, "greeter", agent_id, {"actorChain": chain}
    )
    assert resp.status_code == 200, resp.text
    run_id = _run_started_run_id(resp.text)

    run = await repo.get_run(session, run_id)
    assert run["metadata"]["verifiedActorChain"]["subject"] == subject
    assert run["metadata"]["verifiedActorChain"]["actors"] == [
        {"publicKey": caller.public_key, "agentName": None}
    ]


async def test_agui_run_with_invalid_actor_chain_401s(client, new_identity):
    agent_id_registrant = new_identity()
    await _register(client, agent_id_registrant, "greeter")

    body = _run_input("thread_made_up")
    body["metadata"] = {"actorChain": ["not-a-real-jwt"]}

    resp = await client.post("/agui/greeter", json=body)
    assert resp.status_code == 401


async def test_agui_run_without_actor_chain_is_unaffected(client, session, new_identity):
    agent_id_registrant = new_identity()
    agent_id = await _register(client, agent_id_registrant, "greeter")

    resp = await _offline_run_with_metadata(client, session, "greeter", agent_id, {})
    assert resp.status_code == 200, resp.text
    run_id = _run_started_run_id(resp.text)

    run = await repo.get_run(session, run_id)
    assert "verifiedActorChain" not in run["metadata"]


def test_build_forwarded_props_includes_caller_when_chain_verified():
    from souk.protocols.agui import build_forwarded_props

    subject = {"type": "user", "id": "employee_x"}
    actors = [{"publicKey": "abc", "agentName": None}]
    chain = ["hop0"]

    result = build_forwarded_props(
        "test-signing-secret", "run_1", "agent_1", {}, {"appSpecific": True}, subject, actors, chain
    )

    assert result == {
        "appSpecific": True,
        "caller": {"subject": subject, "actors": actors, "chain": chain},
    }


def test_build_forwarded_props_without_chain_or_kyok_passes_through_untouched():
    from souk.protocols.agui import build_forwarded_props

    result = build_forwarded_props("test-signing-secret", "run_1", "agent_1", {}, {"appSpecific": True})

    assert result == {"appSpecific": True}
