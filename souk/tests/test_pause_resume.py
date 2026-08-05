"""souk/pause.py's waitingOnRunId: run-scoped, single-hop, not transitive,
stable across pause/resume rounds.

Covers four things:
1. The single-hop chain (A calls B calls C): B declaring it's waiting on
   C is enough to surface B once C resolves, but nothing automatically
   carries that notification further up to A unless A made the same
   declaration about B.
2. Why matching is scoped to a specific run_id rather than a whole
   thread: a callee thread can be reused across several unrelated
   delegation calls over its lifetime (see ensure_thread's
   parent_thread_id reuse), so two separate delegations to the same
   thread must never be confused with each other, regardless of the
   order they resolve in.
3. That a paused run resuming keeps its *same* run_id/task_id (see
   repo.reopen_run) rather than handing off to a new one — this is what
   makes (2) hold without any pointer-retargeting machinery, and what
   keeps run_events' seq numbering correct across rounds.
4. That the actual function _handle_finish/_handle_cancel/_handle_fail
   call (grpc_server._resume_parent_run_if_waiting) — not just the
   find_run_waiting_on building block underneath it — does the right
   thing end to end, for both a genuinely-paused waiter and a
   genuinely-completed one (see api_a2a._finalize_delegated_call).
"""

from __future__ import annotations

import json

from souk import repo
from souk.broker import FinishStream, RelayEvent, broker
from souk.grpc_server import _handle_finish, _handle_relay, _resume_parent_run_if_waiting


async def test_single_hop_notification_does_not_propagate_past_a_non_waiting_caller(session, new_identity):
    identity = new_identity()
    agent_ids = await repo.register_agents(
        session, "sdk_1", identity.public_key, [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    )
    agent_a, agent_b, agent_c = agent_ids["a"], agent_ids["b"], agent_ids["c"]

    thread_a = await repo.ensure_thread(session, agent_a, None)
    thread_b = await repo.ensure_thread(session, agent_b, None)
    thread_c = await repo.ensure_thread(session, agent_c, None)

    # A called B synchronously and already treated B's immediate reply as
    # the whole answer — it never declared any interest in B's run.
    run_a = await repo.create_run(session, thread_a, agent_a, "a2a", {})
    await repo.mark_run_status(session, run_a["run_id"], "completed")

    # B explicitly paused, waiting on C's specific run (the sub-agent-call
    # convention).
    run_b1 = await repo.create_run(session, thread_b, agent_b, "a2a", {})
    run_c = await repo.create_run(session, thread_c, agent_c, "a2a", {})
    await repo.mark_run_status(
        session, run_b1["run_id"], "input-required", metadata={"waitingOnRunId": run_c["run_id"]}
    )

    # C finishes: single hop works — B is found.
    await repo.mark_run_status(session, run_c["run_id"], "completed")
    waiting_for_c = await repo.find_run_waiting_on(session, run_c["run_id"])
    assert waiting_for_c is not None
    assert waiting_for_c["run_id"] == run_b1["run_id"]

    # B's own run resumes (same run_id — see repo.reopen_run), processes
    # C's result, and finishes for real this time.
    await repo.reopen_run(session, run_b1["run_id"], {"resume": {}})
    await repo.mark_run_status(session, run_b1["run_id"], "completed")

    # Nothing ever declared waitingOnRunId=run_b1, so B finishing
    # surfaces no one — the chain stops here, A is never notified.
    assert await repo.find_run_waiting_on(session, run_b1["run_id"]) is None


async def test_two_separate_delegations_to_the_same_reused_thread_do_not_cross_wires(session, new_identity):
    """The scenario that motivated scoping by run_id instead of
    thread_id: B delegates to C twice on the same reused child thread.
    The *first* delegation's callee run resolves *after* the second
    delegation is already in flight — matching by thread_id alone (with
    only an "already consumed" flag and insertion order to disambiguate)
    could have picked the wrong one; matching by the specific run_id each
    delegation pointed at cannot.
    """
    identity = new_identity()
    agent_ids = await repo.register_agents(
        session, "sdk_1", identity.public_key, [{"name": "b"}, {"name": "c"}]
    )
    agent_b, agent_c = agent_ids["b"], agent_ids["c"]
    thread_b = await repo.ensure_thread(session, agent_b, None)
    thread_c = await repo.ensure_thread(session, agent_c, None, parent_thread_id=thread_b)

    # First delegation: B's run_b1 (genuinely completed, see
    # api_a2a._finalize_delegated_call) waits on C's run_c1.
    run_b1 = await repo.create_run(session, thread_b, agent_b, "a2a", {})
    await repo.mark_run_status(session, run_b1["run_id"], "completed")
    run_c1 = await repo.create_run(session, thread_c, agent_c, "a2a", {})
    await repo.merge_run_metadata(session, run_b1["run_id"], {"waitingOnRunId": run_c1["run_id"]})

    # Second delegation, later, same thread_c reused: B's run_b2 waits on
    # C's run_c2 — an entirely different run.
    run_b2 = await repo.create_run(session, thread_b, agent_b, "a2a", {})
    await repo.mark_run_status(session, run_b2["run_id"], "completed")
    run_c2 = await repo.create_run(session, thread_c, agent_c, "a2a", {})
    await repo.merge_run_metadata(session, run_b2["run_id"], {"waitingOnRunId": run_c2["run_id"]})

    # run_c2 (the *second* delegation's callee) resolves first.
    await repo.mark_run_status(session, run_c2["run_id"], "completed")
    match = await repo.find_run_waiting_on(session, run_c2["run_id"])
    assert match["run_id"] == run_b2["run_id"]

    # run_c1 (the *first* delegation's callee, still outstanding) must
    # not be confused with run_c2 just because they share a thread.
    match_c1 = await repo.find_run_waiting_on(session, run_c1["run_id"])
    assert match_c1["run_id"] == run_b1["run_id"]


async def test_reopen_run_keeps_the_same_run_id_through_multiple_hitl_rounds(session, new_identity):
    """A waiter's pointer must still be valid however many pause/resume
    rounds the callee goes through — not because the pointer gets
    retargeted, but because reopen_run never changes the run_id being
    pointed at in the first place.
    """
    identity = new_identity()
    agent_ids = await repo.register_agents(
        session, "sdk_1", identity.public_key, [{"name": "b"}, {"name": "c"}]
    )
    agent_b, agent_c = agent_ids["b"], agent_ids["c"]
    thread_b = await repo.ensure_thread(session, agent_b, None)
    thread_c = await repo.ensure_thread(session, agent_c, None, parent_thread_id=thread_b)

    run_b = await repo.create_run(session, thread_b, agent_b, "a2a", {})
    await repo.mark_run_status(session, run_b["run_id"], "completed")
    run_c = await repo.create_run(session, thread_c, agent_c, "a2a", {})
    await repo.merge_run_metadata(session, run_b["run_id"], {"waitingOnRunId": run_c["run_id"]})

    # C's first HITL round: someone approves, the *same* run reopens for
    # another round (the normal human-resume path — api_agui.py/
    # api_a2a.py's own resuming_run_id handling — reproduced directly
    # here) rather than a new run_id being minted.
    await repo.mark_run_status(session, run_c["run_id"], "input-required", metadata={"round": 1})
    await repo.reopen_run(session, run_c["run_id"], {"resume": {}})
    assert (await repo.get_run(session, run_c["run_id"]))["status"] == "queued"

    # A second HITL round on C — still the same run_id.
    await repo.mark_run_status(session, run_c["run_id"], "input-required", metadata={"round": 2})
    await repo.reopen_run(session, run_c["run_id"], {"resume": {}})

    # B's pointer never needed to move — it was always this run_id.
    reread_b = await repo.get_run(session, run_b["run_id"])
    assert reread_b["metadata"]["waitingOnRunId"] == run_c["run_id"]

    # C truly finishes now — still matches by that same run_id.
    await repo.mark_run_status(session, run_c["run_id"], "completed")
    match = await repo.find_run_waiting_on(session, run_c["run_id"])
    assert match["run_id"] == run_b["run_id"]


async def test_reopen_run_lets_run_events_seq_continue_instead_of_colliding(session, new_identity):
    """The in-memory broker.Run object driving each round is a fresh
    object (see broker.RunBroker.enqueue_run's `seq` parameter) — without
    passing it the real last-seen seq, a second round would restart at 0
    and collide with run_events rows the first round already wrote.
    """
    identity = new_identity()
    agent_ids = await repo.register_agents(session, "sdk_1", identity.public_key, [{"name": "c"}])
    agent_c = agent_ids["c"]
    thread_c = await repo.ensure_thread(session, agent_c, None)
    run_c = await repo.create_run(session, thread_c, agent_c, "a2a", {})

    await repo.append_run_event(session, run_c["run_id"], 1, {"type": "TEXT_MESSAGE_CONTENT"})
    await repo.append_run_event(session, run_c["run_id"], 2, {"type": "TEXT_MESSAGE_CONTENT"})
    assert await repo.get_last_event_seq(session, run_c["run_id"]) == 2

    await repo.mark_run_status(session, run_c["run_id"], "input-required")
    await repo.reopen_run(session, run_c["run_id"], {"resume": {}})

    # A round-2 event, correctly continuing from seq=2, not restarting.
    starting_seq = await repo.get_last_event_seq(session, run_c["run_id"])
    await repo.append_run_event(session, run_c["run_id"], starting_seq + 1, {"type": "TEXT_MESSAGE_CONTENT"})

    events = await repo.get_run_events(session, run_c["run_id"])
    assert len(events) == 3


async def test_finalize_delegated_call_marks_the_caller_waiting_when_the_callee_pauses(session, new_identity):
    from souk.api_a2a import _finalize_delegated_call

    identity = new_identity()
    agent_ids = await repo.register_agents(
        session, "sdk_1", identity.public_key, [{"name": "b"}, {"name": "c"}]
    )
    agent_b, agent_c = agent_ids["b"], agent_ids["c"]

    thread_b = await repo.ensure_thread(session, agent_b, None)
    # thread_c is the callee of a delegation from thread_b — same
    # parentThreadId wiring api_a2a._start_run does for a real sub-agent
    # call.
    thread_c = await repo.ensure_thread(session, agent_c, None, parent_thread_id=thread_b)

    run_b = await repo.create_run(session, thread_b, agent_b, "a2a", {})  # B's active run
    run_c = await repo.create_run(session, thread_c, agent_c, "a2a", {})
    await repo.mark_run_status(
        session, run_c["run_id"], "input-required", metadata={"reason": "hitl_approval"}
    )

    db_run = await _finalize_delegated_call(session, run_c["run_id"])
    assert db_run["status"] == "input-required"

    reread_b = await repo.get_run(session, run_b["run_id"])
    # Pinned to C's specific run_id, not its thread — see
    # souk/pause.py's waitingOnRunId docstring.
    assert reread_b["metadata"]["waitingOnRunId"] == run_c["run_id"]
    # B itself was never touched otherwise — still whatever it was
    # (queued here, since this test never advanced it), not paused.
    assert reread_b["status"] == "queued"


async def test_finalize_delegated_call_is_a_no_op_when_the_callee_truly_finished(session, new_identity):
    from souk.api_a2a import _finalize_delegated_call

    identity = new_identity()
    agent_ids = await repo.register_agents(
        session, "sdk_1", identity.public_key, [{"name": "b"}, {"name": "c"}]
    )
    agent_b, agent_c = agent_ids["b"], agent_ids["c"]

    thread_b = await repo.ensure_thread(session, agent_b, None)
    thread_c = await repo.ensure_thread(session, agent_c, None, parent_thread_id=thread_b)

    run_b = await repo.create_run(session, thread_b, agent_b, "a2a", {})
    run_c = await repo.create_run(session, thread_c, agent_c, "a2a", {})
    await repo.mark_run_status(session, run_c["run_id"], "completed")

    await _finalize_delegated_call(session, run_c["run_id"])

    reread_b = await repo.get_run(session, run_b["run_id"])
    assert "waitingOnRunId" not in reread_b["metadata"]


async def test_resume_parent_run_if_waiting_resumes_a_genuinely_paused_parent(session, new_identity):
    """The classic HITL case, end-to-end through the actual function that
    _handle_finish/_handle_cancel/_handle_fail call — not just the
    find_run_waiting_on building block underneath it.
    """
    identity = new_identity()
    agent_ids = await repo.register_agents(
        session, "sdk_1", identity.public_key, [{"name": "b"}, {"name": "c"}]
    )
    agent_b, agent_c = agent_ids["b"], agent_ids["c"]

    thread_b = await repo.ensure_thread(session, agent_b, None)
    thread_c = await repo.ensure_thread(session, agent_c, None)

    run_c = await repo.create_run(session, thread_c, agent_c, "a2a", {})
    run_b1 = await repo.create_run(session, thread_b, agent_b, "a2a", {})
    await repo.mark_run_status(
        session, run_b1["run_id"], "input-required", metadata={"waitingOnRunId": run_c["run_id"]}
    )
    await repo.mark_run_status(session, run_c["run_id"], "completed")

    await _resume_parent_run_if_waiting(run_c["run_id"], thread_c, "completed")

    # Same run_id, reopened — not a new one.
    reread_b1 = await repo.get_run(session, run_b1["run_id"])
    assert reread_b1["status"] == "queued"
    assert reread_b1["input_json"]["resume"] == {
        "waitingOnThreadId": thread_c,
        "status": "completed",
        "result": "",
    }
    assert broker.get(run_b1["run_id"]) is not None
    broker.forget(run_b1["run_id"])


async def test_resume_parent_run_if_waiting_wakes_a_completed_parent_that_delegated_async(
    session, new_identity
):
    """Scenario 2, end-to-end: B never paused (its own row is
    'completed'), souk attached the subscription after the fact (see
    api_a2a._finalize_delegated_call), and C later finishing must still
    reach it through the exact same function _handle_finish calls — not
    just through find_run_waiting_on in isolation.
    """
    identity = new_identity()
    agent_ids = await repo.register_agents(
        session, "sdk_1", identity.public_key, [{"name": "b"}, {"name": "c"}]
    )
    agent_b, agent_c = agent_ids["b"], agent_ids["c"]

    thread_b = await repo.ensure_thread(session, agent_b, None)
    thread_c = await repo.ensure_thread(session, agent_c, None, parent_thread_id=thread_b)

    run_c = await repo.create_run(session, thread_c, agent_c, "a2a", {})
    run_b = await repo.create_run(session, thread_b, agent_b, "a2a", {})
    await repo.mark_run_status(session, run_b["run_id"], "completed")
    await repo.merge_run_metadata(session, run_b["run_id"], {"waitingOnRunId": run_c["run_id"]})

    await repo.mark_run_status(session, run_c["run_id"], "completed")

    await _resume_parent_run_if_waiting(run_c["run_id"], thread_c, "completed")

    reread_b = await repo.get_run(session, run_b["run_id"])
    # Still 'completed' — never rewritten to 'resumed' (see
    # merge_run_metadata's docstring), and its pointer is cleared now
    # that it's been acted on.
    assert reread_b["status"] == "completed"
    assert reread_b["metadata"]["waitingOnRunId"] is None

    # The new run for B is whatever's now active on thread_b.
    new_run = await repo.get_active_run_for_thread(session, thread_b)
    assert new_run is not None
    assert new_run["input_json"]["resume"]["waitingOnThreadId"] == thread_c
    assert broker.get(new_run["run_id"]) is not None
    broker.forget(new_run["run_id"])


async def test_resume_parent_run_if_waiting_sends_full_history_by_default(session, new_identity):
    """The default (AgentHandle.manages_own_history=False, souk-side just
    'no manages_own_history metadata at all') — the parent's real message
    history is re-sent on auto-resume, same as before this flag existed.
    """
    identity = new_identity()
    agent_ids = await repo.register_agents(
        session, "sdk_1", identity.public_key, [{"name": "b"}, {"name": "c"}]
    )
    agent_b, agent_c = agent_ids["b"], agent_ids["c"]

    thread_b = await repo.ensure_thread(session, agent_b, None)
    thread_c = await repo.ensure_thread(session, agent_c, None)

    run_c = await repo.create_run(session, thread_c, agent_c, "a2a", {})
    run_b1 = await repo.create_run(session, thread_b, agent_b, "a2a", {})
    await repo.append_thread_messages(
        session, thread_b, run_b1["run_id"], [{"id": "m1", "role": "user", "content": "hi"}]
    )
    await repo.mark_run_status(
        session, run_b1["run_id"], "input-required", metadata={"waitingOnRunId": run_c["run_id"]}
    )
    await repo.mark_run_status(session, run_c["run_id"], "completed")

    await _resume_parent_run_if_waiting(run_c["run_id"], thread_c, "completed")

    run = broker.get(run_b1["run_id"])
    assert run is not None
    assert [m["id"] for m in run.input_json["messages"]] == ["m1"]
    broker.forget(run_b1["run_id"])


async def test_resume_parent_run_if_waiting_skips_history_for_manages_own_history_agents(
    session, new_identity
):
    """AgentHandle(manages_own_history=True) (souk-agent-sdk) registers as
    agents.metadata.manages_own_history=True — souk must send `messages:
    []` on auto-resume instead of re-dumping the parent thread's history,
    since a provider that opted into this already has it under
    threadId. The delegated call's actual result still arrives, just via
    the `context` entry (see grpc_server._resume_parent_run_if_waiting),
    not a redundant copy in `messages`.
    """
    identity = new_identity()
    agent_ids = await repo.register_agents(
        session,
        "sdk_1",
        identity.public_key,
        [{"name": "b", "metadata": {"manages_own_history": True}}, {"name": "c"}],
    )
    agent_b, agent_c = agent_ids["b"], agent_ids["c"]

    thread_b = await repo.ensure_thread(session, agent_b, None)
    thread_c = await repo.ensure_thread(session, agent_c, None)

    run_c = await repo.create_run(session, thread_c, agent_c, "a2a", {})
    run_b1 = await repo.create_run(session, thread_b, agent_b, "a2a", {})
    await repo.append_thread_messages(
        session, thread_b, run_b1["run_id"], [{"id": "m1", "role": "user", "content": "hi"}]
    )
    await repo.mark_run_status(
        session, run_b1["run_id"], "input-required", metadata={"waitingOnRunId": run_c["run_id"]}
    )
    await repo.mark_run_status(session, run_c["run_id"], "completed")

    await _resume_parent_run_if_waiting(run_c["run_id"], thread_c, "completed")

    run = broker.get(run_b1["run_id"])
    assert run is not None
    assert run.input_json["messages"] == []
    [context_entry] = run.input_json["context"]
    assert context_entry["description"] == "souk.sub_agent_resume"
    assert json.loads(context_entry["value"])["waitingOnThreadId"] == thread_c


async def test_native_ag_ui_interrupt_outcome_pauses_a_run(session, new_identity):
    """The other half of souk/pause.py: a provider's own RUN_FINISHED
    outcome (AG-UI's native interrupt mechanism — no souk-specific CUSTOM
    event involved) end-to-end through the real handlers _handle_relay/
    _handle_finish, not just souk.pause.interrupt_outcome_of in
    isolation (see test_pause.py for that).
    """
    identity = new_identity()
    agent_ids = await repo.register_agents(session, "sdk_1", identity.public_key, [{"name": "b"}])
    agent_b = agent_ids["b"]
    thread_b = await repo.ensure_thread(session, agent_b, None)
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
    thread_b = await repo.ensure_thread(session, agent_b, None)
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
