
"""The health sweeps: what souk does about runs that went wrong on their own.

`fail_unclaimed_runs` used to live here — a database sweep for runs nobody had
claimed. It is gone: the broker holds every queued run in memory along with
when it started waiting, so it answers "how long have I held this" without
reading anything, and gives up on them itself. See
`RunBroker.expire_queued`, and `test_run_failure_is_reported` for what a
caller sees when nobody ever comes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from souk import repo
from souk.schema import agents, runs


async def _make_paused_run(session, agent, thread_id, seconds_stale: int) -> str:
    created = await repo.create_run(session, thread_id, agent, "ag-ui", {"messages": []})
    run_id = created["run_id"]
    await repo.mark_run_status(session, run_id, "input-required", metadata={"interrupts": []})
    await session.execute(
        update(runs)
        .where(runs.c.run_id == run_id)
        .values(last_activity_at=datetime.now(timezone.utc) - timedelta(seconds=seconds_stale))
    )
    await session.commit()
    return run_id


async def test_fail_stale_paused_runs_fails_runs_past_timeout(session, new_identity):
    identity = new_identity()
    registered = await repo.register_agents(session, identity.public_key, [{"name": "translator"}])
    agent = registered["translator"]
    thread_id = await repo.create_thread(session, agent)

    run_id = await _make_paused_run(session, agent, thread_id, seconds_stale=120)

    failed_run_ids = await repo.fail_stale_paused_runs(session, timeout_seconds=60)
    assert failed_run_ids == [run_id]

    run = await repo.get_run(session, run_id)
    assert run.status == "failed"
    assert run.metadata["failureReason"] == "paused_no_resume"


async def test_fail_stale_paused_runs_leaves_recent_pauses_alone(session, new_identity):
    identity = new_identity()
    registered = await repo.register_agents(session, identity.public_key, [{"name": "translator"}])
    agent = registered["translator"]
    thread_id = await repo.create_thread(session, agent)

    run_id = await _make_paused_run(session, agent, thread_id, seconds_stale=1)

    assert await repo.fail_stale_paused_runs(session, timeout_seconds=60) == []

    run = await repo.get_run(session, run_id)
    assert run.status == "input-required"


async def test_fail_stale_paused_runs_ignores_running_and_queued(session, new_identity):
    identity = new_identity()
    registered = await repo.register_agents(session, identity.public_key, [{"name": "translator"}])
    agent = registered["translator"]
    thread_id = await repo.create_thread(session, agent)

    created = await repo.create_run(session, thread_id, agent, "ag-ui", {"messages": []})
    await session.execute(
        update(runs)
        .where(runs.c.run_id == created["run_id"])
        .values(last_activity_at=datetime.now(timezone.utc) - timedelta(seconds=120))
    )
    await session.commit()

    assert await repo.fail_stale_paused_runs(session, timeout_seconds=60) == []

    run = await repo.get_run(session, created["run_id"])
    assert run.status == "queued"


async def test_sweep_once_skips_paused_sweep_when_unconfigured(session, souk, new_identity):
    from souk import health

    assert souk.settings.paused_timeout_seconds is None

    identity = new_identity()
    registered = await repo.register_agents(session, identity.public_key, [{"name": "translator"}])
    agent = registered["translator"]
    thread_id = await repo.create_thread(session, agent)
    run_id = await _make_paused_run(session, agent, thread_id, seconds_stale=10**6)

    await health.sweep_once(souk)

    run = await repo.get_run(session, run_id)
    assert run.status == "input-required"
