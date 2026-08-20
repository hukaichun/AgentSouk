from __future__ import annotations

import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from funduq.errors import RunNotFound
from funduq.identity import (
    InvalidActorChain,
    extend_actor_chain,
    new_actor_chain,
)
from funduq.protocols.a2a import A2AAdapter

from tests.conftest import EchoAgent

USER = {"type": "user", "id": "employee_x"}


def _message(text: str) -> dict:
    return {"role": "user", "parts": [{"type": "text", "text": text}]}


async def test_delegate_without_building_a_json_rpc_envelope(funduq, serve):
    callee = (await serve(EchoAgent(), "callee")).agents["callee"]

    task = await A2AAdapter(funduq).send_task(callee, _message("do the thing"))

    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["id"].startswith("run_")


async def test_the_caller_is_the_declared_shape_on_both_roads(funduq, serve):
    from ag_ui.core import RunAgentInput, UserMessage

    from funduq.props import CallerProps
    from funduq.protocols.agui import AGUIAdapter

    agui_served = await serve(EchoAgent(), "agui-callee")
    a2a_served = await serve(EchoAgent(), "a2a-callee")
    chain = new_actor_chain(Ed25519PrivateKey.generate(), USER)

    stream = await AGUIAdapter(funduq).run(
        agui_served.agents["agui-callee"],
        RunAgentInput(
            thread_id="t-props",
            run_id="r",
            state={},
            messages=[UserMessage(id="m1", role="user", content="hi")],
            tools=[],
            context=[],
            forwarded_props={},
            metadata={"actorChain": chain},
        ),
    )
    async for _ in stream.events:
        pass
    await A2AAdapter(funduq).send_task(a2a_served.agents["a2a-callee"], _message("hi"), actor_chain=chain)

    caller = agui_served.provider.seen_caller
    assert caller is not None
    assert a2a_served.provider.seen_caller == caller
    assert CallerProps.model_validate(caller).model_dump(mode="json", by_alias=True) == caller


async def test_identity_is_carried_through_an_in_process_hop(funduq, serve):
    served = await serve(EchoAgent(), "callee")
    callee, provider = served.agents["callee"], served.provider

    agency, relaying_agent = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    chain = extend_actor_chain(relaying_agent, new_actor_chain(agency, USER))

    await A2AAdapter(funduq).send_task(callee, _message("hi"), actor_chain=chain)

    assert provider.seen_caller["subject"] == USER
    assert [a["publicKey"] for a in provider.seen_caller["actors"]] == [
        agency.public_key().public_bytes_raw().hex(),
        relaying_agent.public_key().public_bytes_raw().hex(),
    ]
    assert provider.seen_caller["chain"] == chain


async def test_a_tampered_chain_is_rejected_on_this_path_too(funduq, serve):
    callee = (await serve(EchoAgent(), "callee")).agents["callee"]

    chain = new_actor_chain(Ed25519PrivateKey.generate(), USER)
    tampered = [chain[0][:-4] + "AAAA"]

    with pytest.raises(InvalidActorChain):
        await A2AAdapter(funduq).send_task(callee, _message("hi"), actor_chain=tampered)


async def test_lineage_links_the_callee_thread_back_to_the_caller(funduq, serve, register):
    caller = (await register("caller")).agents["caller"]
    callee = (await serve(EchoAgent(), "callee")).agents["callee"]

    caller_thread = await funduq.create_thread(caller)
    caller_run = await funduq.start_run(caller, {"messages": []}, thread_id=caller_thread)

    await A2AAdapter(funduq).send_task(
        callee, _message("hi"), reference_task_ids=[caller_run.run_id]
    )

    tree = await funduq.get_thread_tree(caller_thread)
    assert len(tree["children"]) == 1


async def test_context_id_continues_the_same_conversation(funduq, serve):
    callee = (await serve(EchoAgent(), "callee")).agents["callee"]
    adapter = A2AAdapter(funduq)

    first = await adapter.send_task(callee, _message("one"))
    second = await adapter.send_task(
        callee, _message("two"), context_id=first["contextId"]
    )

    assert second["contextId"] == first["contextId"]
    assert second["id"] != first["id"]


async def test_the_wire_rung_and_the_semantic_rung_agree(funduq, serve):
    callee = (await serve(EchoAgent(), "callee")).agents["callee"]
    adapter = A2AAdapter(funduq)

    direct = await adapter.send_task(callee, _message("hi"))
    envelope = await adapter.handle_rpc(
        callee,
        {"jsonrpc": "2.0", "id": "1", "method": "SendMessage", "params": {"message": _message("hi")}},
    )

    assert envelope["jsonrpc"] == "2.0" and envelope["id"] == "1"
    via_wire = envelope["result"]
    assert via_wire.keys() == direct.keys()
    assert via_wire["status"]["state"] == direct["status"]["state"] == "TASK_STATE_COMPLETED"


async def test_get_and_cancel_are_callable_without_an_envelope(funduq, serve):
    callee = (await serve(EchoAgent(), "callee")).agents["callee"]
    adapter = A2AAdapter(funduq)

    task = await adapter.send_task(callee, _message("hi"))
    assert (await adapter.get_task(callee, task["id"]))["id"] == task["id"]

    cancelled = await adapter.cancel_task(callee, task["id"])
    assert cancelled["status"]["state"] == "TASK_STATE_COMPLETED"

    with pytest.raises(RunNotFound):
        await adapter.get_task(callee, "run_does_not_exist")


async def test_an_unknown_task_is_an_error_not_an_exception_over_the_wire(funduq, register):
    callee = (await register("callee")).agents["callee"]
    response = await A2AAdapter(funduq).handle_rpc(
        callee, {"jsonrpc": "2.0", "id": "9", "method": "tasks/get", "params": {"id": "run_nope"}}
    )
    assert response["error"]["message"] == "task not found"
