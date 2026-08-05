"""Covers the AG-UI HTTP surface: POST /threads (the only way to obtain a
thread_id — see api_agui.py's module docstring) and that /agui/... runs
require one, real ag_ui.core.RunAgentInput shape and all.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text


async def _register(client, identity, sdk_client_id, name, **extra):
    body = identity.register_body(sdk_client_id, [{"name": name, **extra}])
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


async def test_create_thread_by_name_and_by_id_return_real_thread_ids(client, new_identity):
    identity = new_identity()
    agent_id = await _register(client, identity, "sdk_1", "greeter")

    by_name = await client.post("/threads/greeter")
    assert by_name.status_code == 200, by_name.text
    assert by_name.json()["thread_id"].startswith("thread_")

    by_id = await client.post(f"/threads/id/{agent_id}")
    assert by_id.status_code == 200, by_id.text
    assert by_id.json()["thread_id"] != by_name.json()["thread_id"]


async def test_create_thread_for_an_unregistered_agent_404s(client):
    resp = await client.post("/threads/id/agent_does_not_exist")
    assert resp.status_code == 404


async def test_agui_run_rejects_an_unknown_thread_id(client, new_identity):
    identity = new_identity()
    await _register(client, identity, "sdk_1", "greeter")

    resp = await client.post("/agui/greeter", json=_run_input("thread_made_up"))
    assert resp.status_code == 404


async def test_agui_run_against_an_offline_agent_fails_fast(client, new_identity, session):
    identity = new_identity()
    agent_id = await _register(client, identity, "sdk_1", "translator")

    await session.execute(
        text("UPDATE agents SET last_seen_at = :ts WHERE agent_id = :id"),
        {"ts": datetime.now(timezone.utc) - timedelta(seconds=120), "id": agent_id},
    )
    await session.commit()

    created = await client.post("/threads/translator")
    thread_id = created.json()["thread_id"]

    resp = await client.post("/agui/translator", json=_run_input(thread_id))
    assert resp.status_code == 200
    assert "X-Souk-Thread-Id" in resp.headers
    assert resp.headers["X-Souk-Thread-Id"] == thread_id
    body_text = resp.text
    assert "RUN_ERROR" in body_text
    assert "offline" in body_text
