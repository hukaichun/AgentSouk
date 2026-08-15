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

from sqlalchemy import update

from souk import repo
from souk.schema import agents, thread_history


async def test_fail_unclaimed_runs_updates_status_and_metadata_without_sql_error(session, new_identity):
    identity = new_identity()
    agent_ids = await repo.register_agents(session, identity.public_key, [{"name": "translator"}])
    agent_id = agent_ids["translator"]

    thread_id = await repo.create_thread(session, agent_id)
    created = await repo.create_run(session, thread_id, agent_id, "a2a", {"messages": []})
    run_id = created["run_id"]

    # Simulate: target went offline, and this run has sat queued past the
    # timeout — both conditions fail_unclaimed_runs requires.
    await session.execute(
        update(agents)
        .where(agents.c.agent_id == agent_id)
        .values(last_seen_at=datetime.now(timezone.utc) - timedelta(seconds=120))
    )
    await session.execute(
        update(thread_history)
        .where(thread_history.c.run_id == run_id)
        .values(created_at=datetime.now(timezone.utc) - timedelta(seconds=120))
    )
    await session.commit()

    failed_run_ids = await repo.fail_unclaimed_runs(session, timeout_seconds=45, online_window_seconds=60)
    assert failed_run_ids == [run_id]

    run = await repo.get_run(session, run_id)
    assert run.status == "failed"
    assert run.metadata["failureReason"] == "no_provider_online"


async def test_fail_unclaimed_runs_leaves_recent_or_online_runs_alone(session, new_identity):
    identity = new_identity()
    agent_ids = await repo.register_agents(session, identity.public_key, [{"name": "translator"}])
    agent_id = agent_ids["translator"]

    thread_id = await repo.create_thread(session, agent_id)
    created = await repo.create_run(session, thread_id, agent_id, "a2a", {"messages": []})

    # Target is still online (last_seen_at untouched, just registered) —
    # even though the run is nominally "queued", nothing should fire.
    assert await repo.fail_unclaimed_runs(session, timeout_seconds=0, online_window_seconds=60) == []

    run = await repo.get_run(session, created["run_id"])
    assert run.status == "queued"


async def _make_paused_run(session, agent_id, thread_id, seconds_stale: int) -> str:
    created = await repo.create_run(session, thread_id, agent_id, "ag-ui", {"messages": []})
    run_id = created["run_id"]
    await repo.mark_run_status(session, run_id, "input-required", metadata={"interrupts": []})
    await session.execute(
        update(thread_history)
        .where(thread_history.c.run_id == run_id)
        .values(last_activity_at=datetime.now(timezone.utc) - timedelta(seconds=seconds_stale))
    )
    await session.commit()
    return run_id


async def test_fail_stale_paused_runs_fails_runs_past_timeout(session, new_identity):
    identity = new_identity()
    agent_ids = await repo.register_agents(session, identity.public_key, [{"name": "translator"}])
    agent_id = agent_ids["translator"]
    thread_id = await repo.create_thread(session, agent_id)

    run_id = await _make_paused_run(session, agent_id, thread_id, seconds_stale=120)

    failed_run_ids = await repo.fail_stale_paused_runs(session, timeout_seconds=60)
    assert failed_run_ids == [run_id]

    run = await repo.get_run(session, run_id)
    assert run.status == "failed"
    assert run.metadata["failureReason"] == "paused_no_resume"


async def test_fail_stale_paused_runs_leaves_recent_pauses_alone(session, new_identity):
    identity = new_identity()
    agent_ids = await repo.register_agents(session, identity.public_key, [{"name": "translator"}])
    agent_id = agent_ids["translator"]
    thread_id = await repo.create_thread(session, agent_id)

    run_id = await _make_paused_run(session, agent_id, thread_id, seconds_stale=1)

    assert await repo.fail_stale_paused_runs(session, timeout_seconds=60) == []

    run = await repo.get_run(session, run_id)
    assert run.status == "input-required"


async def test_fail_stale_paused_runs_ignores_running_and_queued(session, new_identity):
    identity = new_identity()
    agent_ids = await repo.register_agents(session, identity.public_key, [{"name": "translator"}])
    agent_id = agent_ids["translator"]
    thread_id = await repo.create_thread(session, agent_id)

    created = await repo.create_run(session, thread_id, agent_id, "ag-ui", {"messages": []})
    await session.execute(
        update(thread_history)
        .where(thread_history.c.run_id == created["run_id"])
        .values(last_activity_at=datetime.now(timezone.utc) - timedelta(seconds=120))
    )
    await session.commit()

    assert await repo.fail_stale_paused_runs(session, timeout_seconds=60) == []

    run = await repo.get_run(session, created["run_id"])
    assert run.status == "queued"


async def test_sweep_once_skips_paused_sweep_when_unconfigured(session, souk, new_identity):
    from souk import health

    # paused_timeout_seconds=None (the default) means "no timeout at all" —
    # stated by constructing the settings, rather than by monkeypatching a
    # global, now that a Souk carries its own configuration.
    assert souk.settings.paused_timeout_seconds is None

    identity = new_identity()
    agent_ids = await repo.register_agents(session, identity.public_key, [{"name": "translator"}])
    agent_id = agent_ids["translator"]
    thread_id = await repo.create_thread(session, agent_id)
    run_id = await _make_paused_run(session, agent_id, thread_id, seconds_stale=10**6)

    await health.sweep_once(souk)

    run = await repo.get_run(session, run_id)
    assert run.status == "input-required"
