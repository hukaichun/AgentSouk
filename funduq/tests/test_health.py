from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from funduq import repo
from funduq.schema import agents, runs


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


async def test_sweep_once_skips_paused_sweep_when_unconfigured(session, funduq, new_identity):
    from funduq import health

    assert funduq.settings.paused_timeout_seconds is None

    identity = new_identity()
    registered = await repo.register_agents(session, identity.public_key, [{"name": "translator"}])
    agent = registered["translator"]
    thread_id = await repo.create_thread(session, agent)
    run_id = await _make_paused_run(session, agent, thread_id, seconds_stale=10**6)

    await health.sweep_once(funduq)

    run = await repo.get_run(session, run_id)
    assert run.status == "input-required"


async def test_a_reaped_pause_gets_its_terminal_event_persisted(session, funduq, new_identity):
    from funduq.health import close_with_terminal_event

    identity = new_identity()
    registered = await repo.register_agents(session, identity.public_key, [{"name": "reaped"}])
    agent = registered["reaped"]
    thread_id = await repo.create_thread(session, agent)
    run_id = await _make_paused_run(session, agent, thread_id, seconds_stale=120)
    await repo.append_run_event(
        session, run_id, 1, {"type": "RUN_FINISHED", "threadId": thread_id, "runId": run_id}
    )

    await repo.fail_stale_paused_runs(session, timeout_seconds=60)
    await close_with_terminal_event(funduq, run_id, "paused_no_resume")

    events = await repo.get_run_events(session, run_id)
    assert events[-1] == {"type": "RUN_ERROR", "message": "paused_no_resume"}, (
        "the event stream must end the way the database says the run did"
    )


async def test_a_run_that_reported_its_own_error_is_left_alone(session, funduq, new_identity):
    from funduq.health import close_with_terminal_event

    identity = new_identity()
    registered = await repo.register_agents(session, identity.public_key, [{"name": "spoke"}])
    agent = registered["spoke"]
    thread_id = await repo.create_thread(session, agent)
    created = await repo.create_run(session, thread_id, agent, "ag-ui", {})
    run_id = created["run_id"]
    await repo.append_run_event(session, run_id, 1, {"type": "RUN_ERROR", "message": "my own"})
    await repo.mark_run_status(session, run_id, "failed", metadata={"failureReason": "x"})

    await close_with_terminal_event(funduq, run_id, "paused_no_resume")

    events = await repo.get_run_events(session, run_id)
    assert [e["message"] for e in events if e["type"] == "RUN_ERROR"] == ["my own"]


async def test_orphans_reaped_at_start_get_terminal_events(settings, session, new_identity):
    from funduq.core import Funduq

    identity = new_identity()
    registered = await repo.register_agents(session, identity.public_key, [{"name": "orphan"}])
    agent = registered["orphan"]
    thread_id = await repo.create_thread(session, agent)
    created = await repo.create_run(session, thread_id, agent, "ag-ui", {})
    run_id = created["run_id"]

    reborn = Funduq(settings)
    try:
        orphaned = await reborn.start()
        assert run_id in orphaned
        async with reborn.session() as s:
            events = await repo.get_run_events(s, run_id)
            stored = await repo.get_run(s, run_id)
        assert stored.status == "failed"
        assert events[-1] == {"type": "RUN_ERROR", "message": "orphaned_by_funduq_restart"}
    finally:
        await reborn.aclose()
