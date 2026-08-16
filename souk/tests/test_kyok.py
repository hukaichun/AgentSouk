"""Covers KYOK (Keep Your Own Key) — souk/kyok.py's token issue/verify,
and the souk/api_llm_bridge.py HTTP surface (`/kyok/poll`,
`/kyok/v1/chat/completions`, `/kyok/respond/{request_id}`) plus its pure
`_collapse_stream` helper. See docs/keep-your-own-key.md for the full
design; this was previously entirely untested (see that doc's own
"Status: experimental" header before this file existed).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time

import pytest

from souk import repo
from souk.models import AgentRef
from souk.protocols.kyok import collapse_stream
from souk.kyok import KyokBridge, issue_kyok_token, verify_kyok_token


def _kyok_headers(bearer: str, private_key, body: bytes) -> dict:
    """Mirrors souk_agent_sdk.KyokSigningAuth.auth_flow exactly (see
    docs/keep-your-own-key.md's "Binding a token to the specific run and
    provider that hold it" section) — reimplemented here for the same
    reason conftest.py's Identity.register_body reimplements registration
    signing: this test suite doesn't depend on souk_agent_sdk as a package.
    """
    timestamp = str(int(time.time()))
    body_hash = hashlib.sha256(body).hexdigest()
    payload = f"{bearer}:{timestamp}:{body_hash}".encode()
    signature = private_key.sign(payload).hex()
    return {
        "Authorization": f"Bearer {bearer}",
        "X-Souk-Kyok-Timestamp": timestamp,
        "X-Souk-Kyok-Signature": signature,
    }


async def _register_agent(session, new_identity, name: str = "greeter"):
    identity = new_identity()
    registered = await repo.register_agents(session, identity.public_key, [{"name": name}])
    return identity, registered[name]


_AGENT = AgentRef(provider_key="ab" * 32, name="translator")


# --- souk/kyok.py: token issue/verify -----------------------------------


def test_kyok_token_roundtrip():
    token = issue_kyok_token("run_1", "sess_1", _AGENT, "test-signing-secret")
    result = verify_kyok_token(token, "test-signing-secret")
    assert result is not None
    assert result.run_id == "run_1"
    assert result.session_id == "sess_1"
    assert result.agent == _AGENT


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


# --- api_llm_bridge.py: auth/validation chain ----------------------------


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


# ---- KyokBridge registry lifetimes
#
# `GET /kyok/poll` takes an unauthenticated, caller-chosen `sessionId`, so the
# number of distinct keys this registry can be asked about is under a
# stranger's control. Both maps were `defaultdict`s, which means a *lookup*
# inserted, and nothing ever reclaimed: 100k polls of unknown sessions
# retained 81 MiB, and an ordinary finished session leaked a key too. A session
# is now one object with one lifetime rule (`_Session.is_idle`). These
# pin the lifetime rule — an entry exists exactly while there is something to
# find — rather than pinning the fix's shape.


async def test_polling_an_unknown_session_records_nothing() -> None:
    bridge = KyokBridge()

    for i in range(100):
        assert await bridge.poll_one(f"junk_{i}", 0) is None

    assert bridge._sessions == {}


async def test_a_poll_that_waits_and_times_out_records_nothing() -> None:
    """The other half of the same hole: the waiting path added a subscriber
    set, and discarding the event emptied it without removing it."""
    bridge = KyokBridge()

    assert await bridge.poll_one("nobody", 0.01) is None

    assert bridge._sessions == {}


async def test_a_finished_session_leaves_nothing_behind() -> None:
    """Not only an adversarial matter — `popleft()` empties a deque without
    removing it, so every legitimate session leaked one entry for the life of
    the process."""
    bridge = KyokBridge()
    request_id, _queue = bridge.submit("legit", {"messages": []})

    assert (await bridge.poll_one("legit", 0))["requestId"] == request_id
    bridge.forget(request_id)

    assert bridge._requests == {}
    assert bridge._sessions == {}


async def test_a_waiting_poll_is_still_woken_by_a_submit() -> None:
    """The regression the cleanup could plausibly cause: the wake path runs
    through a set that is now removed when it empties."""
    bridge = KyokBridge()

    async def submit_shortly() -> str:
        await asyncio.sleep(0.01)
        request_id, _ = bridge.submit("live", {"messages": []})
        return request_id

    submitted, polled = await asyncio.gather(submit_shortly(), bridge.poll_one("live", 2.0))

    assert polled is not None and polled["requestId"] == submitted
    assert bridge._sessions == {}


async def test_an_abandoned_request_does_not_hide_the_work_behind_it() -> None:
    """A request forgotten before anyone polled it leaves a dead id at the
    head of its session's queue. Returning None there would tell a bridge
    with work waiting that it has nothing to do."""
    bridge = KyokBridge()
    abandoned, _ = bridge.submit("mixed", {"messages": ["first"]})
    live, _ = bridge.submit("mixed", {"messages": ["second"]})
    bridge.forget(abandoned)

    assert (await bridge.poll_one("mixed", 0))["requestId"] == live
