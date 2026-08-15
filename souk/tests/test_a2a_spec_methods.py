"""What souk answers to on the A2A wire, and in which vocabulary.

souk hand-wrote A2A as string literals for a long time, so when the spec
moved nothing broke — it just stopped being reachable. It was still offering
`tasks/send` (the original name) two versions after v1.0 renamed the JSON-RPC
methods to its gRPC service method names, so a client built from the current
schema got `-32601 method not found` on its first call.

souk now emits v1.0 and only v1.0, built from `a2a.types.a2a_pb2`, and
accepts every spelling it has ever offered. These tests are what stands
between that and the next silent drift: the method names come from the
`A2AService` descriptor, so a rename fails at import rather than in
production.
"""

from __future__ import annotations

import pytest

from souk.protocols.a2a import PROTOCOL_VERSION, A2AAdapter, A2AStream

from tests.test_in_process_delegation import Callee, _message, _register


async def _rpc(souk, agent_id: str, method: str, params: dict):
    return await A2AAdapter(souk).handle_rpc(
        agent_id, {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}
    )


@pytest.fixture
async def callee(souk):
    agent_id, key = await _register(souk, "callee")
    await souk.attach_provider(key, Callee(), [agent_id])
    return agent_id


@pytest.mark.parametrize("method", ["SendMessage", "message/send", "tasks/send"])
async def test_send_answers_to_every_name_it_has_ever_had(souk, callee, method):
    """v1.0's name, v0.3's name, and the original. Accepting an old method
    name costs one set lookup and keeps deployed callers working; the *reply*
    is v1.0 regardless, which is the half that had to pick a version."""
    envelope = await _rpc(souk, callee, method, {"message": _message("hi")})

    assert envelope["result"]["status"]["state"] == "TASK_STATE_COMPLETED"


@pytest.mark.parametrize("method", ["SendStreamingMessage", "message/stream", "tasks/sendSubscribe"])
async def test_stream_answers_to_every_name_it_has_ever_had(souk, callee, method):
    result = await _rpc(souk, callee, method, {"message": _message("hi")})

    assert isinstance(result, A2AStream)
    updates = [item["result"] async for item in result.results]
    # Every item is a StreamResponse — v1.0 wraps them, where v0.3 put a bare
    # update on the wire with a `kind` discriminator.
    assert all({"statusUpdate", "artifactUpdate"} & set(u) for u in updates)
    assert updates[-1]["statusUpdate"]["status"]["state"] == "TASK_STATE_COMPLETED"


@pytest.mark.parametrize("method", ["GetTask", "tasks/get"])
async def test_get_answers_to_either_name(souk, callee, method):
    sent = await _rpc(souk, callee, "SendMessage", {"message": _message("hi")})

    got = await _rpc(souk, callee, method, {"id": sent["result"]["id"]})

    assert got["result"]["id"] == sent["result"]["id"]


async def test_an_unknown_method_is_still_method_not_found(souk, callee):
    envelope = await _rpc(souk, callee, "TeleportTask", {"message": _message("hi")})

    assert envelope["error"]["code"] == -32601


async def test_context_id_is_read_off_the_message(souk, callee):
    """SendMessageRequest is `{message, configuration?, metadata?}` — nothing
    else — so contextId travels on the message. souk's first implementation
    read it from the top level, which is still accepted; what matters is that
    the message's own value works."""
    first = await _rpc(souk, callee, "SendMessage", {"message": _message("hi")})
    context_id = first["result"]["contextId"]

    second = await _rpc(
        souk, callee, "SendMessage", {"message": {**_message("again"), "contextId": context_id}}
    )

    assert second["result"]["contextId"] == context_id
    assert second["result"]["id"] != first["result"]["id"]


async def test_task_id_on_the_message_continues_that_task(souk, callee):
    """The other half of the same move: a caller holding a task id can
    continue it without also having kept the contextId."""
    first = await _rpc(souk, callee, "SendMessage", {"message": _message("hi")})
    task_id = first["result"]["id"]

    second = await _rpc(
        souk, callee, "SendMessage", {"message": {**_message("again"), "taskId": task_id}}
    )

    assert second["result"]["contextId"] == first["result"]["contextId"]


async def test_an_unknown_task_id_is_task_not_found_not_a_fresh_thread(souk, callee):
    """`taskId` is a claim about where this message belongs, unlike
    `referenceTaskIds`, which is informational. Silently starting a new
    thread would strand the conversation the caller meant to continue."""
    envelope = await _rpc(
        souk, callee, "SendMessage", {"message": {**_message("hi"), "taskId": "run_nope"}}
    )

    assert envelope["error"]["code"] == -32001


@pytest.mark.parametrize("method", ["SubscribeToTask", "tasks/resubscribe"])
async def test_subscribing_to_a_finished_task_reports_its_outcome(souk, callee, method):
    """Rejoining a stream a moment too late must not be an empty stream —
    that reads exactly like a task still thinking."""
    sent = await _rpc(souk, callee, "SendMessage", {"message": _message("hi")})

    result = await _rpc(souk, callee, method, {"id": sent["result"]["id"]})

    updates = [item["result"] async for item in result.results]
    assert updates[-1]["statusUpdate"]["status"]["state"] == "TASK_STATE_COMPLETED"


async def test_subscribing_to_an_unknown_task_is_an_error(souk, callee):
    envelope = await _rpc(souk, callee, "SubscribeToTask", {"id": "run_nope"})

    assert envelope["error"]["code"] == -32001


async def test_the_agent_card_says_which_spec_this_endpoint_speaks(souk, callee):
    """The card is where a client learns to call `SendMessage` instead of
    probing for a method and getting -32601 — the failure this whole file
    exists because of. v1.0 moved that statement into `supportedInterfaces`,
    each entry carrying its own binding and version."""
    card = await A2AAdapter(souk, "http://souk.example").agent_card(callee)

    assert PROTOCOL_VERSION == "1.0"
    assert card["supportedInterfaces"] == [
        {
            "url": f"http://souk.example/a2a/id/{callee}/rpc",
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
        }
    ]
    assert card["capabilities"]["streaming"] is True
