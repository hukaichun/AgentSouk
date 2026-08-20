from __future__ import annotations

import pytest

from funduq.protocols.a2a import PROTOCOL_VERSION, A2AAdapter, A2AStream, ServedInterface

from tests.conftest import EchoAgent


async def _rpc(funduq, agent, method: str, params: dict):
    return await A2AAdapter(funduq).handle_rpc(
        agent, {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}
    )


def _message(text: str) -> dict:
    return {"role": "user", "parts": [{"type": "text", "text": text}]}


@pytest.fixture
async def callee(serve):
    return (await serve(EchoAgent(), "callee")).agents["callee"]


@pytest.mark.parametrize("method", ["SendMessage", "message/send", "tasks/send"])
async def test_send_answers_to_every_name_it_has_ever_had(funduq, callee, method):
    envelope = await _rpc(funduq, callee, method, {"message": _message("hi")})

    assert envelope["result"]["status"]["state"] == "TASK_STATE_COMPLETED"


@pytest.mark.parametrize("method", ["SendStreamingMessage", "message/stream", "tasks/sendSubscribe"])
async def test_stream_answers_to_every_name_it_has_ever_had(funduq, callee, method):
    result = await _rpc(funduq, callee, method, {"message": _message("hi")})

    assert isinstance(result, A2AStream)
    updates = [item["result"] async for item in result.results]
    assert all({"statusUpdate", "artifactUpdate"} & set(u) for u in updates)
    assert updates[-1]["statusUpdate"]["status"]["state"] == "TASK_STATE_COMPLETED"


@pytest.mark.parametrize("method", ["GetTask", "tasks/get"])
async def test_get_answers_to_either_name(funduq, callee, method):
    sent = await _rpc(funduq, callee, "SendMessage", {"message": _message("hi")})

    got = await _rpc(funduq, callee, method, {"id": sent["result"]["id"]})

    assert got["result"]["id"] == sent["result"]["id"]


async def test_an_unknown_method_is_still_method_not_found(funduq, callee):
    envelope = await _rpc(funduq, callee, "TeleportTask", {"message": _message("hi")})

    assert envelope["error"]["code"] == -32601


async def test_context_id_is_read_off_the_message(funduq, callee):
    first = await _rpc(funduq, callee, "SendMessage", {"message": _message("hi")})
    context_id = first["result"]["contextId"]

    second = await _rpc(
        funduq, callee, "SendMessage", {"message": {**_message("again"), "contextId": context_id}}
    )

    assert second["result"]["contextId"] == context_id
    assert second["result"]["id"] != first["result"]["id"]


async def test_task_id_on_the_message_continues_that_task(funduq, callee):
    first = await _rpc(funduq, callee, "SendMessage", {"message": _message("hi")})
    task_id = first["result"]["id"]

    second = await _rpc(
        funduq, callee, "SendMessage", {"message": {**_message("again"), "taskId": task_id}}
    )

    assert second["result"]["contextId"] == first["result"]["contextId"]


async def test_an_unknown_task_id_is_task_not_found_not_a_fresh_thread(funduq, callee):
    envelope = await _rpc(
        funduq, callee, "SendMessage", {"message": {**_message("hi"), "taskId": "run_nope"}}
    )

    assert envelope["error"]["code"] == -32001


@pytest.mark.parametrize("method", ["SubscribeToTask", "tasks/resubscribe"])
async def test_subscribing_to_a_finished_task_reports_its_outcome(funduq, callee, method):
    sent = await _rpc(funduq, callee, "SendMessage", {"message": _message("hi")})

    result = await _rpc(funduq, callee, method, {"id": sent["result"]["id"]})

    updates = [item["result"] async for item in result.results]
    assert updates[-1]["statusUpdate"]["status"]["state"] == "TASK_STATE_COMPLETED"


async def test_subscribing_to_an_unknown_task_is_an_error(funduq, callee):
    envelope = await _rpc(funduq, callee, "SubscribeToTask", {"id": "run_nope"})

    assert envelope["error"]["code"] == -32001


async def test_the_agent_card_says_which_spec_this_endpoint_speaks(funduq, callee):
    served = ServedInterface(url="https://funduq.example/a2a/ab12/callee/rpc", binding="JSONRPC")
    card = await A2AAdapter(funduq).agent_card(callee, interfaces=[served])

    assert PROTOCOL_VERSION == "1.0"
    assert card["supportedInterfaces"] == [
        {
            "url": "https://funduq.example/a2a/ab12/callee/rpc",
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
        }
    ]
    assert card["capabilities"]["streaming"] is True


async def test_a_card_for_a_funduq_nobody_serves_advertises_nowhere(funduq, callee):
    card = await A2AAdapter(funduq).agent_card(callee)

    assert "supportedInterfaces" not in card
    assert card["capabilities"]["streaming"] is True


async def test_the_agent_card_carries_the_agents_own_version(funduq):
    from tests.conftest import Identity

    identity = Identity()
    signature, timestamp = identity.sign_registration(["versioned"])
    registered = await funduq.register_agents(
        identity.public_key,
        signature,
        timestamp,
        [{"name": "versioned", "agent_card_extra": {"version": "3.1.4"}}],
    )

    card = await A2AAdapter(funduq).agent_card(registered.agents["versioned"])

    assert card["version"] == "3.1.4"
