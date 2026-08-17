
from __future__ import annotations

import asyncio
import base64
import json

import pytest

from souk.identity import kyok_call_signing_payload
from souk_provider_sdk.identity import kyok_call_payload
from souk.models import AgentRef
from souk.protocols.kyok import collapse_stream
from souk.kyok import (
    KyokBridge,
    issue_kyok_token,
    session_routing_key,
    verify_kyok_token,
)


_AGENT = AgentRef(provider_key="ab" * 32, name="translator")


def test_a_kyok_call_signs_what_it_is_for():
    """Every signed payload in this system starts with the operation it
    authorises, so a signature captured for one cannot be presented as
    another (see souk.identity). This one did not, and the only thing
    stopping a collision with a registration was that it would have needed a
    bearer equal to the literal string `souk-register`.
    """
    payload = kyok_call_signing_payload("some-token", 1755300000, "ab" * 32).decode()

    assert payload.startswith("souk-kyok-call:")
    assert payload == f"souk-kyok-call:some-token:1755300000:{'ab' * 32}"


def test_both_sides_state_the_kyok_signing_payload_the_same_way():
    """souk and souk_provider_sdk each write this payload out themselves and
    neither imports the other, which is the point (see that package's
    identity.py): a copy derived from souk agrees with souk by construction
    and therefore checks nothing. Two independent statements only check each
    other if something compares them, and this is that something.

    Unlike registration, no other test compares these implicitly — nothing in
    core's suite plays the KYOK caller yet.
    """
    assert kyok_call_payload("tok", 1755300000, "cafe") == kyok_call_signing_payload(
        "tok", 1755300000, "cafe"
    )


def test_kyok_token_roundtrip():
    token = issue_kyok_token("run_1", "sess_1", _AGENT, "test-signing-secret")
    result = verify_kyok_token(token, "test-signing-secret")
    assert result is not None
    assert result.run_id == "run_1"
    assert result.session_key == session_routing_key("sess_1")
    assert result.agent == _AGENT


def test_a_token_does_not_carry_the_session_id_it_was_minted_for():
    """The token is signed, not sealed, and its reader is the provider — the
    one party KYOK exists to keep away from the caller's key. Anything in here
    is disclosed to it.

    Probed before it was fixed: a provider decoded its own token, read the
    session id, connected as the caller's bridge and was handed another
    provider's completion on that session — its prompt to read, its answer to
    write. The assertion is over the whole decoded body rather than one field,
    so a later addition that reintroduces the id under any name fails here.
    """
    token = issue_kyok_token("run_1", "sess_secret", _AGENT, "test-signing-secret")
    body = json.loads(base64.urlsafe_b64decode(token.split(".", 1)[0].encode()))

    assert "sess_secret" not in json.dumps(body)
    assert body["sessionKey"] == session_routing_key("sess_secret")


def test_expired_kyok_token_rejected(monkeypatch):
    import souk.kyok as kyok_module

    monkeypatch.setattr(kyok_module, "KYOK_TOKEN_TTL_SECONDS", -1)
    token = kyok_module.issue_kyok_token("run_1", "sess_1", _AGENT, "test-signing-secret")
    assert verify_kyok_token(token, "test-signing-secret") is None


def test_tampered_kyok_token_signature_rejected():
    token = issue_kyok_token("run_1", "sess_1", _AGENT, "test-signing-secret")
    body, signature = token.split(".", 1)
    tampered = f"{body}.{'0' * len(signature)}"
    assert verify_kyok_token(tampered, "test-signing-secret") is None


@pytest.mark.parametrize(
    "malformed",
    ["not-a-token-at-all", "onlyonepart", "bm90anNvbg==.deadbeef"],
)
def test_malformed_kyok_token_rejected(malformed):
    assert verify_kyok_token(malformed, "test-signing-secret") is None


def _chunk(content: str = "", role: str | None = None, finish_reason: str | None = None) -> dict:
    delta: dict = {}
    if role:
        delta["role"] = role
    if content:
        delta["content"] = content
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "kyok",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def test_collapse_stream_empty_input():
    result = collapse_stream([])
    assert result["choices"][0]["message"] == {"role": "assistant", "content": ""}
    assert result["choices"][0]["finish_reason"] == "stop"


def test_collapse_stream_single_chunk():
    chunks = [_chunk(content="hello", role="assistant", finish_reason="stop")]
    result = collapse_stream(chunks)
    assert result["choices"] == [
        {"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}
    ]


def test_collapse_stream_multiple_chunks_concatenates_content():
    chunks = [
        _chunk(content="hel", role="assistant"),
        _chunk(content="lo"),
        _chunk(content="", finish_reason="stop"),
    ]
    result = collapse_stream(chunks)
    assert result["choices"][0]["message"]["content"] == "hello"
    assert result["choices"][0]["finish_reason"] == "stop"


def test_collapse_stream_multi_index_reassembles_per_choice():
    chunks = [
        {
            "id": "c1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "kyok",
            "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": "a"}, "finish_reason": None},
                {"index": 1, "delta": {"role": "assistant", "content": "b"}, "finish_reason": None},
            ],
        },
        {
            "id": "c1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "kyok",
            "choices": [
                {"index": 0, "delta": {}, "finish_reason": "stop"},
                {"index": 1, "delta": {}, "finish_reason": "length"},
            ],
        },
    ]
    result = collapse_stream(chunks)
    assert result["choices"][0] == {
        "index": 0,
        "message": {"role": "assistant", "content": "a"},
        "finish_reason": "stop",
    }
    assert result["choices"][1] == {
        "index": 1,
        "message": {"role": "assistant", "content": "b"},
        "finish_reason": "length",
    }


async def test_polling_an_unknown_session_records_nothing() -> None:
    bridge = KyokBridge()

    for i in range(100):
        assert await bridge.poll_one(f"junk_{i}", 0) is None

    assert bridge._sessions == {}


async def test_a_poll_that_waits_and_times_out_records_nothing() -> None:
    bridge = KyokBridge()

    assert await bridge.poll_one("nobody", 0.01) is None

    assert bridge._sessions == {}


async def test_a_finished_session_leaves_nothing_behind() -> None:
    bridge = KyokBridge()
    request_id, _queue = bridge.submit("legit", {"messages": []})

    assert (await bridge.poll_one("legit", 0))["requestId"] == request_id
    bridge.forget(request_id)

    assert bridge._requests == {}
    assert bridge._sessions == {}


async def test_a_waiting_poll_is_still_woken_by_a_submit() -> None:
    bridge = KyokBridge()

    async def submit_shortly() -> str:
        await asyncio.sleep(0.01)
        request_id, _ = bridge.submit("live", {"messages": []})
        return request_id

    submitted, polled = await asyncio.gather(submit_shortly(), bridge.poll_one("live", 2.0))

    assert polled is not None and polled["requestId"] == submitted
    assert bridge._sessions == {}


async def test_an_abandoned_request_does_not_hide_the_work_behind_it() -> None:
    bridge = KyokBridge()
    abandoned, _ = bridge.submit("mixed", {"messages": ["first"]})
    live, _ = bridge.submit("mixed", {"messages": ["second"]})
    bridge.forget(abandoned)

    assert (await bridge.poll_one("mixed", 0))["requestId"] == live
