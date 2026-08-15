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

    async def start(self, run_input: dict):
        self.seen_caller = (run_input.get("forwardedProps") or {}).get("caller")
        return self._events(run_input)

    async def cancel(self, run_id: str) -> None:
        pass

    async def _events(self, run_input: dict):
        yield {"type": "RUN_STARTED", "threadId": run_input["threadId"], "runId": run_input["runId"]}
        yield {"type": "TEXT_MESSAGE_START", "messageId": "m1", "role": "assistant"}
        yield {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "done"}
        yield {"type": "TEXT_MESSAGE_END", "messageId": "m1"}
        yield {"type": "RUN_FINISHED", "threadId": run_input["threadId"], "runId": run_input["runId"]}


async def _register(souk, name: str) -> str:
    key = Ed25519PrivateKey.generate()
    timestamp = int(time.time())
    registration = await souk.register_agents(
        f"sdk_{name}",
        key.public_key().public_bytes_raw().hex(),
        key.sign(registration_signing_payload(f"sdk_{name}", [name], timestamp)).hex(),
        timestamp,
        [{"name": name}],
    )
    return registration.agent_ids[name]


def _message(text: str) -> dict:
    return {"role": "user", "parts": [{"type": "text", "text": text}]}


async def test_delegate_without_building_a_json_rpc_envelope(souk):
    callee_id = await _register(souk, "callee")
    callee = Callee()
    await souk.attach_provider(callee_id, callee)

    task = await A2AAdapter(souk).send_task(callee_id, _message("do the thing"))

    assert task["status"]["state"] == "completed"
    assert task["id"].startswith("run_")


async def test_identity_is_carried_through_an_in_process_hop(souk):
    """The reason extend_actor_chain belongs in core: an agent inside souk
    has to be able to relay who it is acting for, or provenance stops here."""
    callee_id = await _register(souk, "callee")
    callee = Callee()
    await souk.attach_provider(callee_id, callee)

    agency, relaying_agent = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    chain = extend_actor_chain(relaying_agent, new_actor_chain(agency, USER))

    await A2AAdapter(souk).send_task(callee_id, _message("hi"), actor_chain=chain)

    # The callee sees who it is ultimately for, and every actor in between.
    assert callee.seen_caller["subject"] == USER
    assert [a["publicKey"] for a in callee.seen_caller["actors"]] == [
        agency.public_key().public_bytes_raw().hex(),
        relaying_agent.public_key().public_bytes_raw().hex(),
    ]
    # The raw chain travels too, so the callee can extend it further.
    assert callee.seen_caller["chain"] == chain


async def test_a_tampered_chain_is_rejected_on_this_path_too(souk):
    """Entering at the semantic rung is a shortcut past the envelope, not
    past verification."""
    callee_id = await _register(souk, "callee")
    await souk.attach_provider(callee_id, Callee())

    chain = new_actor_chain(Ed25519PrivateKey.generate(), USER)
    tampered = [chain[0][:-4] + "AAAA"]

    with pytest.raises(InvalidActorChain):
        await A2AAdapter(souk).send_task(callee_id, _message("hi"), actor_chain=tampered)


async def test_lineage_links_the_callee_thread_back_to_the_caller(souk):
    caller_id = await _register(souk, "caller")
    callee_id = await _register(souk, "callee")
    await souk.attach_provider(callee_id, Callee())

    caller_thread = await souk.create_thread(caller_id)
    caller_run = await souk.start_run(caller_id, {"messages": []}, thread_id=caller_thread)

    await A2AAdapter(souk).send_task(
        callee_id, _message("hi"), reference_task_ids=[caller_run.run_id]
    )

    tree = await souk.get_thread_tree(caller_thread)
    assert len(tree["children"]) == 1


async def test_context_id_continues_the_same_conversation(souk):
    callee_id = await _register(souk, "callee")
    await souk.attach_provider(callee_id, Callee())
    adapter = A2AAdapter(souk)

    first = await adapter.send_task(callee_id, _message("one"))
    second = await adapter.send_task(
        callee_id, _message("two"), context_id=first["contextId"]
    )

    assert second["contextId"] == first["contextId"]
    assert second["id"] != first["id"]  # a new task, same conversation


async def test_the_wire_rung_and_the_semantic_rung_agree(souk):
    """handle_rpc is a wrapper, not a second implementation — if these ever
    diverge, a remote caller and an in-process one are getting different
    behaviour from the same protocol."""
    callee_id = await _register(souk, "callee")
    await souk.attach_provider(callee_id, Callee())
    adapter = A2AAdapter(souk)

    direct = await adapter.send_task(callee_id, _message("hi"))
    envelope = await adapter.handle_rpc(
        callee_id,
        {"jsonrpc": "2.0", "id": "1", "method": "tasks/send", "params": {"message": _message("hi")}},
    )

    assert envelope["jsonrpc"] == "2.0" and envelope["id"] == "1"
    via_wire = envelope["result"]
    # Same shape and outcome; only the ids differ, being separate tasks.
    assert via_wire.keys() == direct.keys()
    assert via_wire["status"]["state"] == direct["status"]["state"] == "completed"


async def test_get_and_cancel_are_callable_without_an_envelope(souk):
    callee_id = await _register(souk, "callee")
    await souk.attach_provider(callee_id, Callee())
    adapter = A2AAdapter(souk)

    task = await adapter.send_task(callee_id, _message("hi"))
    assert (await adapter.get_task(callee_id, task["id"]))["id"] == task["id"]

    # Already finished, so cancelling changes nothing — and says so honestly.
    cancelled = await adapter.cancel_task(callee_id, task["id"])
    assert cancelled["status"]["state"] == "completed"

    with pytest.raises(RunNotFound):
        await adapter.get_task(callee_id, "run_does_not_exist")


async def test_an_unknown_task_is_an_error_not_an_exception_over_the_wire(souk):
    """The wire rung turns it into a JSON-RPC error, since a caller asking
    about an unknown task is an ordinary answer rather than a failed call."""
    callee_id = await _register(souk, "callee")
    response = await A2AAdapter(souk).handle_rpc(
        callee_id, {"jsonrpc": "2.0", "id": "9", "method": "tasks/get", "params": {"id": "run_nope"}}
    )
    assert response["error"]["message"] == "task not found"
