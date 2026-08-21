"""Responsibility chains, enforced: birth binding, write membership, ask authority.

The design (docs/mechanisms/responsibility-chains.md): a thread whose first
run carries an actor chain binds {segment head, serving provider} at birth;
writing is membership; a chained ask is resolved only by a signature from
its authority set; a session delegation certificate resolves a session
key's signatures to the durable authority. Unbound threads keep the old
open behavior — the whole mechanism is opt-in by carrying a chain.
"""

from __future__ import annotations

import pytest

from funduq import repo
from funduq.errors import ThreadMembershipRequired
from funduq.identity import InvalidResolution
from funduq.protocols.a2a import A2AAdapter

from tests.conftest import EchoAgent


def _message(text: str) -> dict:
    return {"role": "user", "parts": [{"type": "text", "text": text}]}


class AskingAgent:
    """Pauses its first round on an interrupt; any later round completes."""

    def __init__(self) -> None:
        self.rounds: list = []

    async def run_stream(self, agent_name: str, run_input):
        self.rounds.append(run_input)
        ids = {"threadId": run_input.thread_id, "runId": run_input.run_id}
        yield {"type": "RUN_STARTED", **ids}
        if len(self.rounds) == 1:
            yield {
                "type": "RUN_FINISHED",
                **ids,
                "outcome": {
                    "type": "interrupt",
                    "interrupts": [{"id": "int_1", "reason": "question", "message": "which?"}],
                },
            }
        else:
            yield {"type": "RUN_FINISHED", **ids}


async def test_a_chained_thread_binds_its_head_at_birth(funduq, serve, new_identity):
    agent = (await serve(EchoAgent(), "bound")).agents["bound"]
    head = new_identity()

    task = await A2AAdapter(funduq).send_task(
        agent, _message("hi"), actor_chain=[head.sign_chain_hop()]
    )

    async with funduq.session() as session:
        run = await repo.get_run(session, task["id"])
        thread = await repo.get_thread(session, task["contextId"])
    assert run.head_key == head.public_key
    assert thread["head_key"] == head.public_key


async def test_a_non_member_cannot_speak_on_a_bound_thread(funduq, serve, new_identity):
    agent = (await serve(EchoAgent(), "guarded")).agents["guarded"]
    head, stranger = new_identity(), new_identity()

    task = await A2AAdapter(funduq).send_task(
        agent, _message("hi"), actor_chain=[head.sign_chain_hop()]
    )
    thread_id = task["contextId"]

    with pytest.raises(ThreadMembershipRequired):
        await A2AAdapter(funduq).send_task(
            agent, _message("let me in"), context_id=thread_id
        )
    with pytest.raises(ThreadMembershipRequired):
        await A2AAdapter(funduq).send_task(
            agent,
            _message("me neither"),
            context_id=thread_id,
            actor_chain=[stranger.sign_chain_hop()],
        )

    # Members interject freely: the head, and the serving provider's own key.
    again = await A2AAdapter(funduq).send_task(
        agent, _message("still me"), context_id=thread_id,
        actor_chain=[head.sign_chain_hop()],
    )
    assert again["status"]["state"] == "TASK_STATE_COMPLETED"


async def test_an_unbound_thread_stays_open_and_is_never_retroactively_locked(
    funduq, serve, new_identity
):
    agent = (await serve(EchoAgent(), "open")).agents["open"]

    first = await A2AAdapter(funduq).send_task(agent, _message("anonymous opener"))
    thread_id = first["contextId"]

    # A chained writer arriving later does not lock the thread against anyone.
    await A2AAdapter(funduq).send_task(
        agent, _message("chained visitor"), context_id=thread_id,
        actor_chain=[new_identity().sign_chain_hop()],
    )
    third = await A2AAdapter(funduq).send_task(
        agent, _message("still anonymous"), context_id=thread_id
    )
    assert third["status"]["state"] == "TASK_STATE_COMPLETED"

    async with funduq.session() as session:
        thread = await repo.get_thread(session, thread_id)
    assert thread["head_key"] is None


async def test_a_chained_ask_is_resolved_only_by_its_authority(funduq, serve, new_identity):
    provider = AskingAgent()
    agent = (await serve(provider, "asker")).agents["asker"]
    head, impostor = new_identity(), new_identity()

    first = await A2AAdapter(funduq).send_task(
        agent, _message("go"), actor_chain=[head.sign_chain_hop()]
    )
    task_id = first["id"]
    assert first["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"

    # No proof, no answer — even from the head's own chain.
    with pytest.raises(InvalidResolution):
        await A2AAdapter(funduq).send_task(
            agent,
            _message("unproven answer"),
            task_id=task_id,
            actor_chain=[head.sign_chain_hop()],
        )

    # A proof signed by the wrong key is refused.
    bad_signature, bad_ts = impostor.sign_resolution(task_id)
    with pytest.raises(InvalidResolution):
        await A2AAdapter(funduq).send_task(
            agent,
            _message("forged answer"),
            task_id=task_id,
            actor_chain=[head.sign_chain_hop()],
            metadata={
                "resolution": {
                    "publicKey": impostor.public_key,
                    "timestamp": bad_ts,
                    "signature": bad_signature,
                }
            },
        )

    # The head's own signature over funduq-resolve:{run_id}:{timestamp} wins.
    signature, timestamp = head.sign_resolution(task_id)
    answered = await A2AAdapter(funduq).send_task(
        agent,
        _message("the answer"),
        task_id=task_id,
        actor_chain=[head.sign_chain_hop()],
        metadata={
            "resolution": {
                "publicKey": head.public_key,
                "timestamp": timestamp,
                "signature": signature,
            }
        },
    )
    assert answered["id"] == task_id
    assert answered["status"]["state"] == "TASK_STATE_COMPLETED"


async def test_a_session_key_resolves_to_its_durable_authority(funduq, serve, new_identity):
    """The delegation certificate: the durable key D signs once, naming the
    session key SK; SK signs everything after, and funduq resolves SK's
    signatures to D — rights attach to D, SK is a glove."""
    provider = AskingAgent()
    agent = (await serve(provider, "delegated")).agents["delegated"]
    durable, session_key = new_identity(), new_identity()
    certificate = durable.sign_delegation(session_key.public_key)

    first = await A2AAdapter(funduq).send_task(
        agent,
        _message("go"),
        actor_chain=[session_key.sign_chain_hop()],
        metadata={"delegation": certificate},
    )
    task_id = first["id"]

    async with funduq.session() as session:
        run = await repo.get_run(session, task_id)
    assert run.head_key == durable.public_key, "the head resolves through the certificate"

    # A later session: new session key, new certificate, same durable rights.
    session_key_2 = new_identity()
    certificate_2 = durable.sign_delegation(session_key_2.public_key)
    signature, timestamp = session_key_2.sign_resolution(task_id)
    answered = await A2AAdapter(funduq).send_task(
        agent,
        _message("the answer"),
        task_id=task_id,
        actor_chain=[session_key_2.sign_chain_hop()],
        metadata={
            "delegation": certificate_2,
            "resolution": {
                "publicKey": session_key_2.public_key,
                "timestamp": timestamp,
                "signature": signature,
            },
        },
    )
    assert answered["status"]["state"] == "TASK_STATE_COMPLETED"


async def test_the_provider_may_resolve_its_own_agents_ask(funduq, serve, new_identity):
    provider = AskingAgent()
    served = await serve(provider, "stall-kept")
    agent = served.agents["stall-kept"]
    head = new_identity()

    first = await A2AAdapter(funduq).send_task(
        agent, _message("go"), actor_chain=[head.sign_chain_hop()]
    )
    task_id = first["id"]

    keeper = served.identity
    signature, timestamp = keeper.sign_resolution(task_id)
    answered = await A2AAdapter(funduq).send_task(
        agent,
        _message("the keeper answers"),
        task_id=task_id,
        actor_chain=[keeper.sign_chain_hop()],
        metadata={
            "resolution": {
                "publicKey": keeper.public_key,
                "timestamp": timestamp,
                "signature": signature,
            }
        },
    )
    assert answered["status"]["state"] == "TASK_STATE_COMPLETED"


async def test_the_agui_door_guards_a_chained_resume_the_same_way(funduq, serve, new_identity):
    from ag_ui.core import RunAgentInput, UserMessage
    from ag_ui.core.types import ResumeEntry

    from funduq.protocols.agui import AGUIAdapter

    provider = AskingAgent()
    served = await serve(provider, "agui-asker")
    agent = served.agents["agui-asker"]
    head = new_identity()
    adapter = AGUIAdapter(funduq)

    def _body(thread_id: str, text: str, metadata: dict, resume=None) -> RunAgentInput:
        return RunAgentInput(
            thread_id=thread_id, run_id="ignored", state={},
            messages=[UserMessage(id="m1", role="user", content=text)],
            tools=[], context=[], forwarded_props={},
            metadata=metadata, resume=resume,
        )

    first = await adapter.run(
        agent, _body("t-chained", "go", {"actorChain": [head.sign_chain_hop()]})
    )
    events = [e async for e in first.events]
    assert any(e.get("type") == "RUN_FINISHED" for e in events)
    run_id = first.run_id

    answer = [ResumeEntry.model_validate(
        {"interruptId": "int_1", "status": "resolved", "payload": {"answer": 42}}
    )]

    with pytest.raises(InvalidResolution):
        await adapter.run(
            agent,
            _body(first.thread_id, "unproven", {"actorChain": [head.sign_chain_hop()]},
                  resume=answer),
        )

    signature, timestamp = head.sign_resolution(run_id)
    resumed = await adapter.run(
        agent,
        _body(
            first.thread_id,
            "the answer",
            {
                "actorChain": [head.sign_chain_hop()],
                "resolution": {
                    "publicKey": head.public_key,
                    "timestamp": timestamp,
                    "signature": signature,
                },
            },
            resume=answer,
        ),
    )
    final = [e async for e in resumed.events]
    assert any(e.get("type") == "RUN_FINISHED" for e in final)
