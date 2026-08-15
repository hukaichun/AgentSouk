"""Covers the A2A HTTP surface: id vs name routing (including the 409
disambiguation for a name collision) and the offline fast-fail path (A7a)
added this session.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update

from souk import repo
from souk.schema import agents, thread_history


async def _register(client, identity, name, **extra):
    body = identity.register_body([{"name": name, **extra}])
    resp = await client.post("/agents/register", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["agent_ids"][name]


async def test_name_and_id_routes_return_the_same_card(client, new_identity):
    identity = new_identity()
    agent_id = await _register(client, identity, "greeter", description="hi")

    by_name = await client.get("/a2a/greeter/.well-known/agent-card.json")
    by_id = await client.get(f"/a2a/id/{agent_id}/.well-known/agent-card.json")

    assert by_name.status_code == by_id.status_code == 200
    assert by_name.json() == by_id.json()
    # v1.0 replaced the card's single `url` with a list of interfaces, each
    # stating its own binding and protocol version.
    assert by_name.json()["supportedInterfaces"][0]["url"].endswith(f"/a2a/id/{agent_id}/rpc")


async def test_the_pre_v1_card_path_is_not_served(client, new_identity):
    """Only the current path. The old one was served for a while, answering
    with the *new* body — which has no top-level `url`, so a pre-v1 client
    found a card it could not use to locate the RPC endpoint. That is not an
    accommodation, and whether to offer a real one is a gateway decision, not
    this library's."""
    agent_id = await _register(client, new_identity(), "greeter", description="hi")

    resp = await client.get(f"/a2a/id/{agent_id}/.well-known/agent.json")

    assert resp.status_code == 404


async def test_ambiguous_name_409s_with_candidates_while_id_routes_still_work(client, new_identity):
    a, b = new_identity(), new_identity()
    id_a = await _register(client, a, "greeter")
    id_b = await _register(client, b, "greeter")

    resp = await client.get("/a2a/greeter/.well-known/agent-card.json")
    assert resp.status_code == 409
    candidate_ids = {c["agent_id"] for c in resp.json()["detail"]["candidates"]}
    assert candidate_ids == {id_a, id_b}

    for agent_id in (id_a, id_b):
        resp = await client.get(f"/a2a/id/{agent_id}/.well-known/agent-card.json")
        assert resp.status_code == 200


async def test_offline_target_fails_fast_instead_of_queueing(client, new_identity, session):
    identity = new_identity()
    agent_id = await _register(client, identity, "translator")

    await session.execute(
        update(agents)
        .where(agents.c.agent_id == agent_id)
        .values(last_seen_at=datetime.now(timezone.utc) - timedelta(seconds=120))
    )
    await session.commit()

    thread_id = await repo.create_thread(session, agent_id)
    await session.commit()

    resp = await client.post(
        f"/a2a/id/{agent_id}/rpc",
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "method": "tasks/send",
            "params": {
                "contextId": thread_id,
                "message": {"role": "user", "parts": [{"type": "text", "text": "hi"}]},
            },
        },
    )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["status"]["state"] == "TASK_STATE_FAILED"

    run = (
        await session.execute(
            select(thread_history.c.status, thread_history.c.metadata).where(
                thread_history.c.run_id == result["id"],
                thread_history.c.kind == "run_status",
            )
        )
    ).mappings().first()
    assert run["status"] == "failed"
    assert run["metadata"]["failureReason"] == "agent_offline"


async def test_a2a_can_never_bypass_a_paused_run_even_with_a_resume_flag(client, new_identity, session):
    """A2A has no resume mechanism at all — see souk/pause.py's module
    docstring for why that's deliberate (an agent must never be the one
    resolving another provider's interrupt). A second tasks/send on the
    same session, even one that tries the old metadata.resume=true
    convention, must not bypass an active, paused run — it just gets
    told the current state back, exactly like a plain duplicate call.
    """
    identity = new_identity()
    agent_id = await _register(client, identity, "approver")

    # Built directly via repo, not through a live tasks/send — that would
    # block draining a run nothing ever claims/finishes (see
    # test_offline_target_fails_fast_instead_of_queueing for the same
    # reason the *other* test sidesteps this differently).
    thread_id = await repo.create_thread(session, agent_id)
    created = await repo.create_run(session, thread_id, agent_id, "a2a", {})
    await repo.mark_run_status(
        session, created["run_id"], "input-required", metadata={"interrupts": [{"id": "int_1"}]}
    )
    await session.commit()

    second = await client.post(
        f"/a2a/id/{agent_id}/rpc",
        json={
            "jsonrpc": "2.0",
            "id": "2",
            "method": "tasks/send",
            "params": {
                "id": "task_b",
                "contextId": thread_id,
                "metadata": {"resume": True},
                "message": {"role": "user", "parts": [{"type": "text", "text": "approved"}]},
            },
        },
    )
    assert second.status_code == 200
    result = second.json()["result"]
    # Still the *original* run — a new one never started.
    assert result["id"] == created["run_id"]
    assert result["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"

    still_one_run = (
        await session.execute(
            select(func.count()).select_from(thread_history).where(
                thread_history.c.kind == "run_status"
            )
        )
    ).scalar()
    assert still_one_run == 1
