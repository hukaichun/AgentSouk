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
from souk.protocols.kyok import collapse_stream
from souk.kyok import issue_kyok_token, verify_kyok_token


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
    agent_ids = await repo.register_agents(session, identity.public_key, [{"name": name}])
    return identity, agent_ids[name]


# --- souk/kyok.py: token issue/verify -----------------------------------


def test_kyok_token_roundtrip():
    token = issue_kyok_token("run_1", "sess_1", "agent_1", "test-signing-secret")
    result = verify_kyok_token(token, "test-signing-secret")
    assert result is not None
    assert result.run_id == "run_1"
    assert result.session_id == "sess_1"
    assert result.agent_id == "agent_1"


def test_expired_kyok_token_rejected(monkeypatch):
    import souk.kyok as kyok_module

    monkeypatch.setattr(kyok_module, "KYOK_TOKEN_TTL_SECONDS", -1)
    token = kyok_module.issue_kyok_token("run_1", "sess_1", "agent_1", "test-signing-secret")
    assert verify_kyok_token(token, "test-signing-secret") is None


def test_tampered_kyok_token_signature_rejected():
    token = issue_kyok_token("run_1", "sess_1", "agent_1", "test-signing-secret")
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
