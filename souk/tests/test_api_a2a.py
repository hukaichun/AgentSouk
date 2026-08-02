"""Covers the A2A HTTP surface: id vs name routing (including the 409
disambiguation for a name collision) and the offline fast-fail path (A7a)
added this session.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text


async def _register(client, identity, sdk_client_id, name, **extra):
    body = identity.register_body(sdk_client_id, [{"name": name, **extra}])
    resp = await client.post("/agents/register", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["agent_ids"][name]


async def test_name_and_id_routes_return_the_same_card(client, new_identity):
    identity = new_identity()
    agent_id = await _register(client, identity, "sdk_1", "greeter", description="hi")

    by_name = await client.get("/a2a/greeter/.well-known/agent.json")
    by_id = await client.get(f"/a2a/id/{agent_id}/.well-known/agent.json")

    assert by_name.status_code == by_id.status_code == 200
    assert by_name.json() == by_id.json()
    assert by_name.json()["url"].endswith(f"/a2a/id/{agent_id}/rpc")


async def test_ambiguous_name_409s_with_candidates_while_id_routes_still_work(client, new_identity):
    a, b = new_identity(), new_identity()
    id_a = await _register(client, a, "sdk_a", "greeter")
    id_b = await _register(client, b, "sdk_b", "greeter")

    resp = await client.get("/a2a/greeter/.well-known/agent.json")
    assert resp.status_code == 409
    candidate_ids = {c["agent_id"] for c in resp.json()["detail"]["candidates"]}
    assert candidate_ids == {id_a, id_b}

    for agent_id in (id_a, id_b):
        resp = await client.get(f"/a2a/id/{agent_id}/.well-known/agent.json")
        assert resp.status_code == 200


async def test_offline_target_fails_fast_instead_of_queueing(client, new_identity, session):
    identity = new_identity()
    agent_id = await _register(client, identity, "sdk_1", "translator")

    await session.execute(
        text("UPDATE agents SET last_seen_at = :ts WHERE agent_id = :id"),
        {"ts": datetime.now(timezone.utc) - timedelta(seconds=120), "id": agent_id},
    )
    await session.commit()

    resp = await client.post(
        f"/a2a/id/{agent_id}/rpc",
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "method": "tasks/send",
            "params": {
                "id": "task_test_offline",
                "message": {"role": "user", "parts": [{"type": "text", "text": "hi"}]},
            },
        },
    )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["status"]["state"] == "failed"

    run = (
        await session.execute(
            text(
                "SELECT status, metadata FROM thread_history "
                "WHERE task_id = 'task_test_offline' AND kind = 'run_status'"
            )
        )
    ).mappings().first()
    assert run["status"] == "failed"
    assert run["metadata"]["failureReason"] == "agent_offline"
