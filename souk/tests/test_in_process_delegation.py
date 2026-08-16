"""One agent delegating to another, from inside souk, with no wire.

The adapters expose every rung rather than only the outermost one, the way
pydantic-ai's UIAdapter does — so a caller that is already in this process
enters at the semantic rung (`send_task`) instead of constructing
`{"jsonrpc": "2.0", ...}` to talk to itself. Both rungs run the same code, so
an in-process delegation and a remote one cannot drift apart.

What has to survive that shortcut is everything souk actually guarantees:
identity is carried forward and verified, lineage is recorded, and the
delegating agent gets an honest answer.
"""

from __future__ import annotations

import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from souk.errors import RunNotFound
from souk.identity import (
    InvalidActorChain,
    extend_actor_chain,
    new_actor_chain,
    registration_signing_payload,
)
from souk.protocols.a2a import A2AAdapter

USER = {"type": "user", "id": "employee_x"}


class Callee:
    """The delegated-to agent. Records the chain it was handed, which is how
    these tests check identity actually arrived."""

    def __init__(self) -> None:
        self.seen_caller: dict | None = None

    async def run_stream(self, agent_id: str, run_input: dict):
        self.seen_caller = (run_input.get("forwardedProps") or {}).get("caller")
        yield {"type": "RUN_STARTED", "threadId": run_input["threadId"], "runId": run_input["runId"]}
        yield {"type": "TEXT_MESSAGE_START", "messageId": "m1", "role": "assistant"}
        yield {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "done"}
        yield {"type": "TEXT_MESSAGE_END", "messageId": "m1"}
        yield {"type": "RUN_FINISHED", "threadId": run_input["threadId"], "runId": run_input["runId"]}


async def _register(souk, name: str) -> AgentRef:
    """The agent that was registered.

    One value, not two: an `AgentRef` is `(provider_key, name)`, so the key
    attaching needs is already in it. This used to hand back the pair
    `(agent_id, public_key)` because the id said nothing about whose it was.
    """
    key = Ed25519PrivateKey.generate()
    public_key = key.public_key().public_bytes_raw().hex()
    timestamp = int(time.time())
    registration = await souk.register_agents(
        public_key,
        key.sign(registration_signing_payload([name], timestamp)).hex(),
        timestamp,
        [{"name": name}],
    )
    return registration.agents[name]


def _message(text: str) -> dict:
    """Deliberately still the *original* part spelling (`type`, not v1.0's
    bare `text` key). souk emits v1.0 but accepts every version it has ever
    offered, and these tests are where that stays true — every delegation
    below is an old-shaped caller getting a correct answer."""
    return {"role": "user", "parts": [{"type": "text", "text": text}]}


async def test_delegate_without_building_a_json_rpc_envelope(souk):
    callee = await _register(souk, "callee")
    await souk.attach_provider(callee.provider_key, Callee(), [callee.name])

    task = await A2AAdapter(souk).send_task(callee, _message("do the thing"))

    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["id"].startswith("run_")


async def test_identity_is_carried_through_an_in_process_hop(souk):
    """The reason extend_actor_chain belongs in core: an agent inside souk
    has to be able to relay who it is acting for, or provenance stops here."""
    callee = await _register(souk, "callee")
    provider = Callee()
    await souk.attach_provider(callee.provider_key, provider, [callee.name])

    agency, relaying_agent = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    chain = extend_actor_chain(relaying_agent, new_actor_chain(agency, USER))

    await A2AAdapter(souk).send_task(callee, _message("hi"), actor_chain=chain)

    # The callee sees who it is ultimately for, and every actor in between.
    assert provider.seen_caller["subject"] == USER
    assert [a["publicKey"] for a in provider.seen_caller["actors"]] == [
        agency.public_key().public_bytes_raw().hex(),
        relaying_agent.public_key().public_bytes_raw().hex(),
    ]
    # The raw chain travels too, so the callee can extend it further.
    assert provider.seen_caller["chain"] == chain


async def test_a_tampered_chain_is_rejected_on_this_path_too(souk):
    """Entering at the semantic rung is a shortcut past the envelope, not
    past verification."""
    callee = await _register(souk, "callee")
    await souk.attach_provider(callee.provider_key, Callee(), [callee.name])

    chain = new_actor_chain(Ed25519PrivateKey.generate(), USER)
    tampered = [chain[0][:-4] + "AAAA"]

    with pytest.raises(InvalidActorChain):
        await A2AAdapter(souk).send_task(callee, _message("hi"), actor_chain=tampered)


async def test_lineage_links_the_callee_thread_back_to_the_caller(souk):
    caller = await _register(souk, "caller")
    callee = await _register(souk, "callee")
    await souk.attach_provider(callee.provider_key, Callee(), [callee.name])

    caller_thread = await souk.create_thread(caller)
    caller_run = await souk.start_run(caller, {"messages": []}, thread_id=caller_thread)

    await A2AAdapter(souk).send_task(
        callee, _message("hi"), reference_task_ids=[caller_run.run_id]
    )

    tree = await souk.get_thread_tree(caller_thread)
    assert len(tree["children"]) == 1


async def test_context_id_continues_the_same_conversation(souk):
    callee = await _register(souk, "callee")
    await souk.attach_provider(callee.provider_key, Callee(), [callee.name])
    adapter = A2AAdapter(souk)

    first = await adapter.send_task(callee, _message("one"))
    second = await adapter.send_task(
        callee, _message("two"), context_id=first["contextId"]
    )

    assert second["contextId"] == first["contextId"]
    assert second["id"] != first["id"]  # a new task, same conversation


async def test_the_wire_rung_and_the_semantic_rung_agree(souk):
    """handle_rpc is a wrapper, not a second implementation — if these ever
    diverge, a remote caller and an in-process one are getting different
    behaviour from the same protocol."""
    callee = await _register(souk, "callee")
    await souk.attach_provider(callee.provider_key, Callee(), [callee.name])
    adapter = A2AAdapter(souk)

    direct = await adapter.send_task(callee, _message("hi"))
    envelope = await adapter.handle_rpc(
        callee,
        {"jsonrpc": "2.0", "id": "1", "method": "SendMessage", "params": {"message": _message("hi")}},
    )

    assert envelope["jsonrpc"] == "2.0" and envelope["id"] == "1"
    via_wire = envelope["result"]
    # Same shape and outcome; only the ids differ, being separate tasks.
    assert via_wire.keys() == direct.keys()
    assert via_wire["status"]["state"] == direct["status"]["state"] == "TASK_STATE_COMPLETED"


async def test_get_and_cancel_are_callable_without_an_envelope(souk):
    callee = await _register(souk, "callee")
    await souk.attach_provider(callee.provider_key, Callee(), [callee.name])
    adapter = A2AAdapter(souk)

    task = await adapter.send_task(callee, _message("hi"))
    assert (await adapter.get_task(callee, task["id"]))["id"] == task["id"]

    # Already finished, so cancelling changes nothing — and says so honestly.
    cancelled = await adapter.cancel_task(callee, task["id"])
    assert cancelled["status"]["state"] == "TASK_STATE_COMPLETED"

    with pytest.raises(RunNotFound):
        await adapter.get_task(callee, "run_does_not_exist")


async def test_an_unknown_task_is_an_error_not_an_exception_over_the_wire(souk):
    """The wire rung turns it into a JSON-RPC error, since a caller asking
    about an unknown task is an ordinary answer rather than a failed call."""
    callee = await _register(souk, "callee")
    response = await A2AAdapter(souk).handle_rpc(
        callee, {"jsonrpc": "2.0", "id": "9", "method": "tasks/get", "params": {"id": "run_nope"}}
    )
    assert response["error"]["message"] == "task not found"
