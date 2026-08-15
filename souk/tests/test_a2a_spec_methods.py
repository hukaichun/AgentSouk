"""What souk answers to, on the A2A wire.

`tasks/send` and `tasks/sendSubscribe` are the *original* A2A method names.
The published spec renamed them to `message/send` and `message/stream` when
sending a message stopped being modelled as creating a task — and souk went
on offering only the old names, because nothing here tracks the spec: souk
depends on `ag-ui-protocol` for AG-UI, but A2A is hand-written, so there is
no version to bump and no import to break. A real client built against the
current schema got `-32601 method not found`.

So these tests are the thing that would otherwise have caught it: the method
names souk accepts, and the places the current spec moved fields to.
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


@pytest.mark.parametrize("method", ["message/send", "tasks/send"])
async def test_send_answers_under_the_current_and_the_original_name(souk, callee, method):
    """Both, deliberately. The rename is what a spec-current client needs;
    keeping the old name is what every already-deployed souk caller needs,
    and it costs one set lookup."""
    envelope = await _rpc(souk, callee, method, {"message": _message("hi")})

    assert envelope["result"]["status"]["state"] == "completed"
    assert envelope["result"]["kind"] == "task"


@pytest.mark.parametrize("method", ["message/stream", "tasks/sendSubscribe"])
async def test_stream_answers_under_the_current_and_the_original_name(souk, callee, method):
    result = await _rpc(souk, callee, method, {"message": _message("hi")})

    assert isinstance(result, A2AStream)
    updates = [item["result"] async for item in result.results]
    assert updates[0]["kind"] == "status-update"
    assert updates[-1]["final"] is True
    # Every update carries taskId/contextId, which is where the spec moved
    # them — souk used to put the task id in a bare `id`.
    assert all("taskId" in u and "contextId" in u for u in updates)


async def test_an_unknown_method_is_still_method_not_found(souk, callee):
    envelope = await _rpc(souk, callee, "tasks/teleport", {"message": _message("hi")})

    assert envelope["error"]["code"] == -32601


async def test_context_id_is_read_off_the_message(souk, callee):
    """MessageSendParams is `{message, configuration?, metadata?}` — nothing
    else — so a spec-current client puts contextId on the message. souk's
    first implementation read it from the top level, which is still accepted;
    what matters is that the message's own value works at all."""
    first = await _rpc(souk, callee, "message/send", {"message": _message("hi")})
    context_id = first["result"]["contextId"]

    second = await _rpc(
        souk, callee, "message/send", {"message": {**_message("again"), "contextId": context_id}}
    )

    assert second["result"]["contextId"] == context_id
    assert second["result"]["id"] != first["result"]["id"]


async def test_task_id_on_the_message_continues_that_task(souk, callee):
    """The other half of the same move: a caller holding a task id can
    continue it without also having kept the contextId."""
    first = await _rpc(souk, callee, "message/send", {"message": _message("hi")})
    task_id = first["result"]["id"]

    second = await _rpc(
        souk, callee, "message/send", {"message": {**_message("again"), "taskId": task_id}}
    )

    assert second["result"]["contextId"] == first["result"]["contextId"]


async def test_an_unknown_task_id_is_task_not_found_not_a_fresh_thread(souk, callee):
    """`taskId` is a claim about where this message belongs, unlike
    `referenceTaskIds`, which is informational. Silently starting a new
    thread would strand the conversation the caller meant to continue."""
    envelope = await _rpc(
        souk, callee, "message/send", {"message": {**_message("hi"), "taskId": "run_nope"}}
    )

    assert envelope["error"]["code"] == -32001


async def test_resubscribe_to_a_finished_task_reports_its_outcome(souk, callee):
    """Rejoining a stream a moment too late must not be an empty stream —
    that reads exactly like a task still thinking."""
    sent = await _rpc(souk, callee, "message/send", {"message": _message("hi")})
    task_id = sent["result"]["id"]

    result = await _rpc(souk, callee, "tasks/resubscribe", {"id": task_id})

    updates = [item["result"] async for item in result.results]
    assert updates[-1]["status"]["state"] == "completed"
    assert updates[-1]["final"] is True


async def test_resubscribe_to_an_unknown_task_is_an_error(souk, callee):
    envelope = await _rpc(souk, callee, "tasks/resubscribe", {"id": "run_nope"})

    assert envelope["error"]["code"] == -32001


async def test_the_agent_card_says_which_spec_this_endpoint_speaks(souk, callee):
    """The card is where a client learns to call `message/send` instead of
    probing for a method and getting -32601 — which is the failure this whole
    file exists because of."""
    card = await A2AAdapter(souk, "http://souk.example").agent_card(callee)

    assert card["protocolVersion"] == PROTOCOL_VERSION == "0.3.0"
    assert card["preferredTransport"] == "JSONRPC"
    assert card["capabilities"]["streaming"] is True
