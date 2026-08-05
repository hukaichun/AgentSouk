"""repo.fail_unclaimed_runs (A7b, the sweep-based fallback for a run that
went dark mid-queue) — caught a real bug live: the UPDATE joins
thread_history and agents, both of which have their own `metadata` column,
and an unqualified `metadata` reference in the SET clause is ambiguous to
Postgres. None of the other tests exercised this path (they only cover
the synchronous fast-fail in api_a2a/api_agui), so it only surfaced once
the background health sweep actually ran against a real queued row.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from souk import repo


async def test_fail_unclaimed_runs_updates_status_and_metadata_without_sql_error(session, new_identity):
    identity = new_identity()
    agent_ids = await repo.register_agents(session, "sdk_1", identity.public_key, [{"name": "translator"}])
    agent_id = agent_ids["translator"]

    thread_id = await repo.create_thread(session, agent_id)
    created = await repo.create_run(session, thread_id, agent_id, "a2a", {"messages": []})
    run_id = created["run_id"]

    # Simulate: target went offline, and this run has sat queued past the
    # timeout — both conditions fail_unclaimed_runs requires.
    await session.execute(
        text("UPDATE agents SET last_seen_at = :ts WHERE agent_id = :id"),
        {"ts": datetime.now(timezone.utc) - timedelta(seconds=120), "id": agent_id},
    )
    await session.execute(
        text("UPDATE thread_history SET created_at = :ts WHERE run_id = :run_id"),
        {"ts": datetime.now(timezone.utc) - timedelta(seconds=120), "run_id": run_id},
    )
    await session.commit()

    failed_run_ids = await repo.fail_unclaimed_runs(session, timeout_seconds=45)
    assert failed_run_ids == [run_id]

    run = await repo.get_run(session, run_id)
    assert run["status"] == "failed"
    assert run["metadata"]["failureReason"] == "no_provider_online"


async def test_fail_unclaimed_runs_leaves_recent_or_online_runs_alone(session, new_identity):
    identity = new_identity()
    agent_ids = await repo.register_agents(session, "sdk_1", identity.public_key, [{"name": "translator"}])
    agent_id = agent_ids["translator"]

    thread_id = await repo.create_thread(session, agent_id)
    created = await repo.create_run(session, thread_id, agent_id, "a2a", {"messages": []})

    # Target is still online (last_seen_at untouched, just registered) —
    # even though the run is nominally "queued", nothing should fire.
    assert await repo.fail_unclaimed_runs(session, timeout_seconds=0) == []

    run = await repo.get_run(session, created["run_id"])
    assert run["status"] == "queued"
