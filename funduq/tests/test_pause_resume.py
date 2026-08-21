from __future__ import annotations

import json

from funduq import repo
from funduq.broker import FinishStream, RelayEvent
from funduq.handlers import _handle_finish, _handle_relay


async def test_native_ag_ui_interrupt_outcome_pauses_a_run(session, funduq, new_identity):
    identity = new_identity()
    registered = await repo.register_agents(session, identity.public_key, [{"name": "b"}])
    agent_b = registered["b"]
    thread_b = await repo.create_thread(session, agent_b)
    created = await repo.create_run(session, thread_b, agent_b, "ag-ui", {})
    await repo.mark_run_status(session, created["run_id"], "running")
    run_id = created["run_id"]
    await session.commit()

    run = funduq.broker.enqueue_run(run_id, agent_b, thread_b, {}, "ag-ui")
    interrupt = {"id": "int_1", "reason": "tool_call", "message": "Approve foo(1)?"}
    finished_event = {
        "type": "RUN_FINISHED",
        "threadId": thread_b,
        "runId": run_id,
        "outcome": {"type": "interrupt", "interrupts": [interrupt]},
    }
    await _handle_relay(funduq, run, RelayEvent(finished_event))
    await _handle_finish(funduq, run, FinishStream())

    reread = await repo.get_run(session, run_id)
    assert reread.status == "input-required"
    assert reread.metadata["interrupts"] == [interrupt]
    funduq.broker.forget(run_id)


async def test_native_ag_ui_success_outcome_completes_a_run_normally(session, funduq, new_identity):
    identity = new_identity()
    registered = await repo.register_agents(session, identity.public_key, [{"name": "b"}])
    agent_b = registered["b"]
    thread_b = await repo.create_thread(session, agent_b)
    created = await repo.create_run(session, thread_b, agent_b, "ag-ui", {})
    await repo.mark_run_status(session, created["run_id"], "running")
    run_id = created["run_id"]
    await session.commit()

    run = funduq.broker.enqueue_run(run_id, agent_b, thread_b, {}, "ag-ui")
    finished_event = {"type": "RUN_FINISHED", "threadId": thread_b, "runId": run_id}
    await _handle_relay(funduq, run, RelayEvent(finished_event))
    await _handle_finish(funduq, run, FinishStream())

    reread = await repo.get_run(session, run_id)
    assert reread.status == "completed"
    funduq.broker.forget(run_id)


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
    await repo.mark_run_status(session, run_c["run_id"], "running")
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
    await repo.mark_run_status(session, run_c["run_id"], "running")
    await repo.mark_run_status(session, run_c["run_id"], "input-required")

    still_active = await repo.get_active_run_for_thread(session, thread_c)
    assert still_active is not None
    assert still_active["run_id"] == run_c["run_id"]

    # The answer arrives: the reply lane reopens the run, it runs again, and
    # completes — the legal road out of a pause.
    assert await repo.reopen_run(
        session, run_c["run_id"], {}, expected_status="input-required"
    )
    await repo.mark_run_status(session, run_c["run_id"], "running")
    await repo.mark_run_status(session, run_c["run_id"], "completed")

    assert await repo.get_active_run_for_thread(session, thread_c) is None


async def test_a_result_with_no_pending_ask_is_refused_not_smuggled(funduq, serve):
    """The door has two entrances: an utterance, or a deferred call's result.
    A resume payload is a result; with nothing paused on the thread there is
    no ask for it to land on, and it must be refused with the thread's state
    — never quietly repackaged as a fresh run carrying an answer the agent
    never asked for."""
    from ag_ui.core import RunAgentInput, UserMessage
    from ag_ui.core.types import ResumeEntry

    from funduq.protocols.agui import AGUIAdapter, ThreadSnapshot
    from tests.conftest import EchoAgent

    served = await serve(EchoAgent(), "grounded")
    agent = served.agents["grounded"]
    adapter = AGUIAdapter(funduq)

    first = await adapter.run(
        agent,
        RunAgentInput(
            thread_id="t-fresh", run_id="ignored", state={},
            messages=[UserMessage(id="m1", role="user", content="hi")],
            tools=[], context=[], forwarded_props={},
        ),
    )
    [event async for event in first.events]

    ghost = await adapter.run(
        agent,
        RunAgentInput(
            thread_id=first.thread_id, run_id="ignored", state={},
            messages=[UserMessage(id="m2", role="user", content="the answer")],
            tools=[], context=[], forwarded_props={},
            resume=[ResumeEntry.model_validate(
                {"interruptId": "int_ghost", "status": "resolved", "payload": {"answer": 42}}
            )],
        ),
    )
    assert isinstance(ghost, ThreadSnapshot), "a result with no ask gets the thread's state back"
