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


async def test_chat_completions_without_bearer_401s(client):
    resp = await client.post("/kyok/v1/chat/completions", content=b"{}")
    assert resp.status_code == 401


async def test_chat_completions_with_invalid_token_401s(client):
    resp = await client.post(
        "/kyok/v1/chat/completions", content=b"{}", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert resp.status_code == 401


async def test_chat_completions_run_not_in_broker_403s(client, session, souk, new_identity):
    identity, agent_id = await _register_agent(session, new_identity)
    token = issue_kyok_token("run_never_started", "sess_1", agent_id, "test-signing-secret")
    resp = await client.post(
        "/kyok/v1/chat/completions", content=b"{}", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403


async def test_chat_completions_agent_id_mismatch_403s(client, session, souk, new_identity):
    identity, agent_id = await _register_agent(session, new_identity)
    run_id = "run_mismatch"
    souk.broker.enqueue_run(run_id, agent_id, "thread_1", {}, "ag-ui")
    try:
        token = issue_kyok_token(run_id, "sess_1", "some_other_agent_id", "test-signing-secret")
        resp = await client.post(
            "/kyok/v1/chat/completions", content=b"{}", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 403
    finally:
        souk.broker.forget(run_id)


async def test_chat_completions_cancelled_run_403s(client, session, souk, new_identity):
    identity, agent_id = await _register_agent(session, new_identity)
    run_id = "run_cancelled"
    run = souk.broker.enqueue_run(run_id, agent_id, "thread_1", {}, "ag-ui")
    run.cancel_requested = True
    try:
        token = issue_kyok_token(run_id, "sess_1", agent_id, "test-signing-secret")
        resp = await client.post(
            "/kyok/v1/chat/completions", content=b"{}", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 403
    finally:
        souk.broker.forget(run_id)


async def test_chat_completions_missing_signature_headers_401s(client, session, souk, new_identity):
    identity, agent_id = await _register_agent(session, new_identity)
    run_id = "run_no_sig"
    souk.broker.enqueue_run(run_id, agent_id, "thread_1", {}, "ag-ui")
    try:
        token = issue_kyok_token(run_id, "sess_1", agent_id, "test-signing-secret")
        resp = await client.post(
            "/kyok/v1/chat/completions", content=b"{}", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 401
    finally:
        souk.broker.forget(run_id)


async def test_chat_completions_stale_timestamp_401s(client, session, souk, new_identity):
    identity, agent_id = await _register_agent(session, new_identity)
    run_id = "run_stale"
    souk.broker.enqueue_run(run_id, agent_id, "thread_1", {}, "ag-ui")
    try:
        token = issue_kyok_token(run_id, "sess_1", agent_id, "test-signing-secret")
        body = b"{}"
        body_hash = hashlib.sha256(body).hexdigest()
        stale_timestamp = str(int(time.time()) - 3600)
        payload = f"{token}:{stale_timestamp}:{body_hash}".encode()
        signature = identity._key.sign(payload).hex()
        resp = await client.post(
            "/kyok/v1/chat/completions",
            content=body,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Souk-Kyok-Timestamp": stale_timestamp,
                "X-Souk-Kyok-Signature": signature,
            },
        )
        assert resp.status_code == 401
    finally:
        souk.broker.forget(run_id)


async def test_chat_completions_malformed_timestamp_401s(client, session, souk, new_identity):
    identity, agent_id = await _register_agent(session, new_identity)
    run_id = "run_malformed_ts"
    souk.broker.enqueue_run(run_id, agent_id, "thread_1", {}, "ag-ui")
    try:
        token = issue_kyok_token(run_id, "sess_1", agent_id, "test-signing-secret")
        resp = await client.post(
            "/kyok/v1/chat/completions",
            content=b"{}",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Souk-Kyok-Timestamp": "not-a-number",
                "X-Souk-Kyok-Signature": "deadbeef",
            },
        )
        assert resp.status_code == 401
    finally:
        souk.broker.forget(run_id)


async def test_chat_completions_unregistered_agent_403s(client, session, souk, new_identity):
    """The token names a real, live run — but the agent behind it was
    never actually registered (or has since been delisted), so there's
    no public_key on file to verify the call-time signature against.
    """
    identity, _real_agent_id = await _register_agent(session, new_identity)
    run_id = "run_unregistered"
    souk.broker.enqueue_run(run_id, "agent_does_not_exist", "thread_1", {}, "ag-ui")
    try:
        token = issue_kyok_token(run_id, "sess_1", "agent_does_not_exist", "test-signing-secret")
        body = b"{}"
        headers = _kyok_headers(token, identity._key, body)
        resp = await client.post("/kyok/v1/chat/completions", content=body, headers=headers)
        assert resp.status_code == 403
    finally:
        souk.broker.forget(run_id)


async def test_chat_completions_bad_signature_401s(client, session, souk, new_identity):
    identity, agent_id = await _register_agent(session, new_identity)
    run_id = "run_bad_sig"
    souk.broker.enqueue_run(run_id, agent_id, "thread_1", {}, "ag-ui")
    try:
        token = issue_kyok_token(run_id, "sess_1", agent_id, "test-signing-secret")
        body = b"{}"
        headers = _kyok_headers(token, identity._key, body)
        headers["X-Souk-Kyok-Signature"] = "00" * 64
        resp = await client.post("/kyok/v1/chat/completions", content=body, headers=headers)
        assert resp.status_code == 401
    finally:
        souk.broker.forget(run_id)


# --- Full success round trip ---------------------------------------------


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


async def test_full_round_trip_non_streaming(client, session, souk, new_identity):
    identity, agent_id = await _register_agent(session, new_identity)
    run_id = "run_success_nonstream"
    session_id = "sess_success_nonstream"
    souk.broker.enqueue_run(run_id, agent_id, "thread_1", {}, "ag-ui")
    try:
        token = issue_kyok_token(run_id, session_id, agent_id, "test-signing-secret")
        body = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()
        headers = {**_kyok_headers(token, identity._key, body), "content-type": "application/json"}

        async def provider_call():
            resp = await client.post("/kyok/v1/chat/completions", content=body, headers=headers)
            assert resp.status_code == 200, resp.text
            return resp.json()

        async def bridge_relay():
            poll_resp = await client.get("/kyok/poll", params={"sessionId": session_id, "waitSeconds": 5})
            assert poll_resp.status_code == 200
            requests = poll_resp.json()["requests"]
            assert len(requests) == 1
            request_id = requests[0]["requestId"]
            ndjson = (
                json.dumps(_chunk(content="hello", role="assistant")) + "\n"
                + json.dumps(_chunk(content=" world", finish_reason="stop")) + "\n"
            )
            respond_resp = await client.post(f"/kyok/respond/{request_id}", content=ndjson)
            assert respond_resp.status_code == 200

        result, _ = await asyncio.gather(provider_call(), bridge_relay())
        assert result["choices"][0]["message"]["content"] == "hello world"
        assert result["choices"][0]["message"]["role"] == "assistant"
        assert result["choices"][0]["finish_reason"] == "stop"
    finally:
        souk.broker.forget(run_id)


async def test_full_round_trip_streaming(client, session, souk, new_identity):
    identity, agent_id = await _register_agent(session, new_identity)
    run_id = "run_success_stream"
    session_id = "sess_success_stream"
    souk.broker.enqueue_run(run_id, agent_id, "thread_1", {}, "ag-ui")
    try:
        token = issue_kyok_token(run_id, session_id, agent_id, "test-signing-secret")
        body = json.dumps({"messages": [{"role": "user", "content": "hi"}], "stream": True}).encode()
        headers = {**_kyok_headers(token, identity._key, body), "content-type": "application/json"}

        async def provider_call():
            async with client.stream(
                "POST", "/kyok/v1/chat/completions", content=body, headers=headers
            ) as resp:
                assert resp.status_code == 200
                return [line async for line in resp.aiter_lines() if line]

        async def bridge_relay():
            poll_resp = await client.get("/kyok/poll", params={"sessionId": session_id, "waitSeconds": 5})
            requests = poll_resp.json()["requests"]
            request_id = requests[0]["requestId"]
            ndjson = json.dumps(_chunk(content="hi", role="assistant", finish_reason="stop")) + "\n"
            await client.post(f"/kyok/respond/{request_id}", content=ndjson)

        lines, _ = await asyncio.gather(provider_call(), bridge_relay())
        assert lines[-1] == "data: [DONE]"
        assert any("hi" in line for line in lines[:-1])
    finally:
        souk.broker.forget(run_id)


async def test_respond_error_line_surfaces_as_error_and_stream_ends(client, session, souk, new_identity):
    identity, agent_id = await _register_agent(session, new_identity)
    run_id = "run_error_line"
    session_id = "sess_error_line"
    souk.broker.enqueue_run(run_id, agent_id, "thread_1", {}, "ag-ui")
    try:
        token = issue_kyok_token(run_id, session_id, agent_id, "test-signing-secret")
        body = json.dumps({"messages": [], "stream": True}).encode()
        headers = {**_kyok_headers(token, identity._key, body), "content-type": "application/json"}

        async def provider_call():
            async with client.stream(
                "POST", "/kyok/v1/chat/completions", content=body, headers=headers
            ) as resp:
                return [line async for line in resp.aiter_lines() if line]

        async def bridge_relay():
            poll_resp = await client.get("/kyok/poll", params={"sessionId": session_id, "waitSeconds": 5})
            request_id = poll_resp.json()["requests"][0]["requestId"]
            ndjson = json.dumps({"error": "upstream LLM call failed"}) + "\n"
            await client.post(f"/kyok/respond/{request_id}", content=ndjson)

        lines, _ = await asyncio.gather(provider_call(), bridge_relay())
        assert len(lines) == 1
        assert json.loads(lines[0].removeprefix("data: ")) == {"error": "upstream LLM call failed"}
    finally:
        souk.broker.forget(run_id)


async def test_claim_timeout_returns_502(client, session, souk, new_identity, monkeypatch):
    import souk.protocols.kyok as kyok_protocol

    monkeypatch.setattr(kyok_protocol, "CLAIM_TIMEOUT_SECONDS", 0.05)
    identity, agent_id = await _register_agent(session, new_identity)
    run_id = "run_claim_timeout"
    souk.broker.enqueue_run(run_id, agent_id, "thread_1", {}, "ag-ui")
    try:
        token = issue_kyok_token(run_id, "sess_unclaimed", agent_id, "test-signing-secret")
        body = json.dumps({"messages": []}).encode()
        headers = {**_kyok_headers(token, identity._key, body), "content-type": "application/json"}
        resp = await client.post("/kyok/v1/chat/completions", content=body, headers=headers)
        assert resp.status_code == 502
    finally:
        souk.broker.forget(run_id)


async def test_respond_unknown_request_id_404s(client):
    resp = await client.post("/kyok/respond/does-not-exist", content=b"")
    assert resp.status_code == 404


# --- _collapse_stream (pure function) ------------------------------------
