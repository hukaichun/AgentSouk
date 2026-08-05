"""Two independent things, covered here:

1. AG-UI's native HITL pause (RUN_FINISHED outcome={"type": "interrupt"})
   end to end through the real handlers _handle_relay/_handle_finish —
   see souk/pause.py's module docstring.
2. Waiting on a sub-agent call is *not* a pause at all: whether a later
   call to the same callee gets a real answer or another "still pending"
   is decided fresh each time, purely by whether the callee's own thread
   can currently accept a new run (repo.get_active_run_for_thread) — no
   subscription, no auto-resume, nothing for a delegating agent to
   declare. api_a2a._finalize_delegated_call just reads back the
   callee's honest status; it registers no interest in it anywhere.
"""

from __future__ import annotations

import json

from souk import repo
from souk.api_a2a import _finalize_delegated_call
from souk.broker import FinishStream, RelayEvent, broker
from souk.grpc_server import _handle_finish, _handle_relay


async def test_native_ag_ui_interrupt_outcome_pauses_a_run(session, new_identity):
    """A provider's own RUN_FINISHED outcome (AG-UI's native interrupt
    mechanism, no souk-specific CUSTOM event involved) end-to-end through
    the real handlers _handle_relay/_handle_finish, not just
    souk.pause.interrupt_outcome_of in isolation (see test_pause.py for
    that).
    """
    identity = new_identity()
    agent_ids = await repo.register_agents(session, "sdk_1", identity.public_key, [{"name": "b"}])
    agent_b = agent_ids["b"]
    thread_b = await repo.create_thread(session, agent_b)
    created = await repo.create_run(session, thread_b, agent_b, "ag-ui", {})
    run_id = created["run_id"]
    await session.commit()

    run = broker.enqueue_run(run_id, agent_b, thread_b, {}, "ag-ui")
    interrupt = {"id": "int_1", "reason": "tool_call", "message": "Approve foo(1)?"}
    finished_event = {
        "type": "RUN_FINISHED",
        "threadId": thread_b,
        "runId": run_id,
        "outcome": {"type": "interrupt", "interrupts": [interrupt]},
    }
    await _handle_relay(run, RelayEvent(json_payload=json.dumps(finished_event)))
    await _handle_finish(run, FinishStream())

    reread = await repo.get_run(session, run_id)
    assert reread["status"] == "input-required"
    assert reread["metadata"]["interrupts"] == [interrupt]
    broker.forget(run_id)


async def test_native_ag_ui_success_outcome_completes_a_run_normally(session, new_identity):
    """Regression guard: a plain RUN_FINISHED (outcome absent, or
    {"type": "success"}) must still complete normally — the interrupt
    check must not fire on the common case.
    """
    identity = new_identity()
    agent_ids = await repo.register_agents(session, "sdk_1", identity.public_key, [{"name": "b"}])
    agent_b = agent_ids["b"]
    thread_b = await repo.create_thread(session, agent_b)
    created = await repo.create_run(session, thread_b, agent_b, "ag-ui", {})
    run_id = created["run_id"]
    await session.commit()

    run = broker.enqueue_run(run_id, agent_b, thread_b, {}, "ag-ui")
    finished_event = {"type": "RUN_FINISHED", "threadId": thread_b, "runId": run_id}
    await _handle_relay(run, RelayEvent(json_payload=json.dumps(finished_event)))
    await _handle_finish(run, FinishStream())

    reread = await repo.get_run(session, run_id)
    assert reread["status"] == "completed"
    broker.forget(run_id)


async def test_finalize_delegated_call_reports_honestly_without_registering_any_interest(
    session, new_identity
):
    """The callee (C) paused (input-required) — _finalize_delegated_call
    must report that honestly, but must not write any bookkeeping onto
    the delegating run (B) or anywhere else. There is nothing left in
    this codebase called "waitingOnRunId" for it to write.
    """
    identity = new_identity()
    agent_ids = await repo.register_agents(
        session, "sdk_1", identity.public_key, [{"name": "b"}, {"name": "c"}]
    )
    agent_b, agent_c = agent_ids["b"], agent_ids["c"]

    thread_b = await repo.create_thread(session, agent_b)
    thread_c = await repo.ensure_thread(session, agent_c, None, parent_thread_id=thread_b)

    run_b = await repo.create_run(session, thread_b, agent_b, "a2a", {})
    run_c = await repo.create_run(session, thread_c, agent_c, "a2a", {})
    await repo.mark_run_status(
        session, run_c["run_id"], "input-required", metadata={"reason": "hitl_approval"}
    )

    db_run = await _finalize_delegated_call(session, run_c["run_id"])
    assert db_run["status"] == "input-required"

    # B was never touched — still whatever it was before this call.
    reread_b = await repo.get_run(session, run_b["run_id"])
    assert reread_b["metadata"] == {}
    assert reread_b["status"] == "queued"


async def test_a_delegating_agent_gets_an_honest_answer_by_just_asking_again(session, new_identity):
    """No subscription, no auto-resume: whether a second call to the same
    callee thread gets the real answer or another "still pending" is
    decided fresh, purely by whether that thread can currently accept a
    new run — see repo.get_active_run_for_thread. This is the entire
    mechanism a delegating agent relies on to eventually get C's answer.
    """
    identity = new_identity()
    agent_ids = await repo.register_agents(session, "sdk_1", identity.public_key, [{"name": "c"}])
    agent_c = agent_ids["c"]
    thread_c = await repo.create_thread(session, agent_c)

    run_c = await repo.create_run(session, thread_c, agent_c, "a2a", {})
    await repo.mark_run_status(session, run_c["run_id"], "input-required")

    # First check-in: C is still working on it — the thread can't accept
    # a new run yet, so whoever's asking gets told to check back later.
    still_active = await repo.get_active_run_for_thread(session, thread_c)
    assert still_active is not None
    assert still_active["run_id"] == run_c["run_id"]

    # C genuinely finishes.
    await repo.mark_run_status(session, run_c["run_id"], "completed")

    # Same query, same thread, no prior subscription of any kind — the
    # honest answer flips the instant the real state does.
    assert await repo.get_active_run_for_thread(session, thread_c) is None
