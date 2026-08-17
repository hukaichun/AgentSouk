from __future__ import annotations

import json

from souk import repo
from souk.broker import FinishStream, RelayEvent
from souk.handlers import _handle_finish, _handle_relay


async def test_native_ag_ui_interrupt_outcome_pauses_a_run(session, souk, new_identity):
    identity = new_identity()
    registered = await repo.register_agents(session, identity.public_key, [{"name": "b"}])
    agent_b = registered["b"]
    thread_b = await repo.create_thread(session, agent_b)
    created = await repo.create_run(session, thread_b, agent_b, "ag-ui", {})
    run_id = created["run_id"]
    await session.commit()

    run = souk.broker.enqueue_run(run_id, agent_b, thread_b, {}, "ag-ui")
    interrupt = {"id": "int_1", "reason": "tool_call", "message": "Approve foo(1)?"}
    finished_event = {
        "type": "RUN_FINISHED",
        "threadId": thread_b,
        "runId": run_id,
        "outcome": {"type": "interrupt", "interrupts": [interrupt]},
    }
    await _handle_relay(souk, run, RelayEvent(finished_event))
    await _handle_finish(souk, run, FinishStream())

    reread = await repo.get_run(session, run_id)
    assert reread.status == "input-required"
    assert reread.metadata["interrupts"] == [interrupt]
    souk.broker.forget(run_id)


async def test_native_ag_ui_success_outcome_completes_a_run_normally(session, souk, new_identity):
    identity = new_identity()
    registered = await repo.register_agents(session, identity.public_key, [{"name": "b"}])
    agent_b = registered["b"]
    thread_b = await repo.create_thread(session, agent_b)
    created = await repo.create_run(session, thread_b, agent_b, "ag-ui", {})
    run_id = created["run_id"]
    await session.commit()

    run = souk.broker.enqueue_run(run_id, agent_b, thread_b, {}, "ag-ui")
    finished_event = {"type": "RUN_FINISHED", "threadId": thread_b, "runId": run_id}
    await _handle_relay(souk, run, RelayEvent(finished_event))
    await _handle_finish(souk, run, FinishStream())

    reread = await repo.get_run(session, run_id)
    assert reread.status == "completed"
    souk.broker.forget(run_id)


async def test_finalize_delegated_call_reports_honestly_without_registering_any_interest(
    session, new_identity
):
    identity = new_identity()
    registered = await repo.register_agents(session, identity.public_key, [{"name": "b"}, {"name": "c"}]
    )
    agent_b, agent_c = registered["b"], registered["c"]

    thread_b = await repo.create_thread(session, agent_b)
    thread_c = await repo.ensure_thread(session, agent_c, None, parent_thread_id=thread_b)

    run_b = await repo.create_run(session, thread_b, agent_b, "a2a", {})
    run_c = await repo.create_run(session, thread_c, agent_c, "a2a", {})
    await repo.mark_run_status(
        session, run_c["run_id"], "input-required", metadata={"reason": "hitl_approval"}
    )

    db_run = await repo.get_run(session, run_c["run_id"])
    assert db_run.status == "input-required"

    reread_b = await repo.get_run(session, run_b["run_id"])
    assert reread_b.metadata == {}
    assert reread_b.status == "queued"


async def test_a_delegating_agent_gets_an_honest_answer_by_just_asking_again(session, new_identity):
    identity = new_identity()
    registered = await repo.register_agents(session, identity.public_key, [{"name": "c"}])
    agent_c = registered["c"]
    thread_c = await repo.create_thread(session, agent_c)

    run_c = await repo.create_run(session, thread_c, agent_c, "a2a", {})
    await repo.mark_run_status(session, run_c["run_id"], "input-required")

    still_active = await repo.get_active_run_for_thread(session, thread_c)
    assert still_active is not None
    assert still_active["run_id"] == run_c["run_id"]

    await repo.mark_run_status(session, run_c["run_id"], "completed")

    assert await repo.get_active_run_for_thread(session, thread_c) is None
