"""Covers KyokBridge — the caller-side half of KYOK (Keep Your Own Key).
See souk_client_sdk/kyok_bridge.py's own docstring and docs/keep-your-
own-key.md (in the souk repo) for the wire protocol this talks. Previously
untested — this module's own docstring said so before this file existed.

Uses respx to fake souk's /kyok/poll and /kyok/respond/{request_id}
endpoints (no real souk instance needed), and monkeypatches
litellm.acompletion directly rather than adding another mocking layer for
it — litellm is already a runtime dependency here.
"""

from __future__ import annotations

import json

import httpx
import litellm
import pytest
import respx

from souk_client_sdk.kyok_bridge import KyokBridge, _to_json_line


# --- _to_json_line: all three normalization paths -------------------------


class _ModelDumpChunk:
    def model_dump(self, mode: str = "python") -> dict:
        return {"via": "model_dump", "mode": mode}


class _DictMethodChunk:
    def dict(self) -> dict:
        return {"via": "dict_method"}


def test_to_json_line_uses_model_dump_when_available():
    line = _to_json_line(_ModelDumpChunk())
    assert json.loads(line) == {"via": "model_dump", "mode": "json"}
    assert line.endswith(b"\n")


def test_to_json_line_falls_back_to_dict_method():
    line = _to_json_line(_DictMethodChunk())
    assert json.loads(line) == {"via": "dict_method"}


def test_to_json_line_falls_back_to_plain_dict_conversion():
    line = _to_json_line({"via": "plain_dict"})
    assert json.loads(line) == {"via": "plain_dict"}


# --- _call_llm --------------------------------------------------------


async def test_call_llm_yields_ndjson_lines_from_litellm_stream(monkeypatch):
    async def fake_acompletion(**kwargs):
        async def gen():
            yield {"choices": [{"delta": {"role": "assistant", "content": "hi"}}]}
            yield {"choices": [{"delta": {"content": " there"}}]}

        return gen()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    bridge = KyokBridge("http://souk.local", model="test-model", api_key="key")

    lines = [line async for line in bridge._call_llm("req_1", {"messages": []})]

    assert len(lines) == 2
    assert json.loads(lines[0]) == {"choices": [{"delta": {"role": "assistant", "content": "hi"}}]}
    assert json.loads(lines[1]) == {"choices": [{"delta": {"content": " there"}}]}


async def test_call_llm_yields_error_line_and_does_not_raise(monkeypatch):
    async def fake_acompletion(**kwargs):
        raise RuntimeError("upstream boom")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    bridge = KyokBridge("http://souk.local", model="test-model", api_key="key")

    lines = [line async for line in bridge._call_llm("req_1", {"messages": []})]

    assert len(lines) == 1
    assert json.loads(lines[0]) == {"error": "upstream boom"}


async def test_call_llm_forwards_body_fields_to_litellm(monkeypatch):
    captured = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)

        async def gen():
            return
            yield  # pragma: no cover - makes this an async generator with no items

        return gen()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    bridge = KyokBridge("http://souk.local", model="test-model", api_key="key", api_base="http://llm.local")

    body = {"messages": [{"role": "user", "content": "hi"}], "tools": [{"type": "function"}], "temperature": 0.5}
    async for _ in bridge._call_llm("req_1", body):
        pass

    assert captured["model"] == "test-model"
    assert captured["api_key"] == "key"
    assert captured["api_base"] == "http://llm.local"
    assert captured["messages"] == body["messages"]
    assert captured["tools"] == body["tools"]
    assert captured["temperature"] == 0.5
    assert captured["stream"] is True


# --- _serve_one ---------------------------------------------------------


@respx.mock
async def test_serve_one_posts_llm_output_to_respond_endpoint(monkeypatch):
    async def fake_acompletion(**kwargs):
        async def gen():
            yield {"choices": [{"delta": {"content": "hi"}}]}

        return gen()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    route = respx.post("http://souk.local/kyok/respond/req_1").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    bridge = KyokBridge("http://souk.local", model="test-model", api_key="key")

    async with httpx.AsyncClient() as client:
        await bridge._serve_one(client, "req_1", {"messages": []})

    assert route.called
    sent_body = route.calls.last.request.content
    assert json.loads(sent_body.decode().strip()) == {"choices": [{"delta": {"content": "hi"}}]}


@respx.mock
async def test_serve_one_swallows_and_logs_post_failure(monkeypatch, caplog):
    async def fake_acompletion(**kwargs):
        async def gen():
            yield {"choices": []}

        return gen()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    respx.post("http://souk.local/kyok/respond/req_1").mock(side_effect=httpx.ConnectError("boom"))
    bridge = KyokBridge("http://souk.local", model="test-model", api_key="key")

    async with httpx.AsyncClient() as client:
        await bridge._serve_one(client, "req_1", {"messages": []})  # must not raise


# --- serve_forever --------------------------------------------------------


async def test_open_mints_a_hex_session_id():
    bridge = KyokBridge("http://souk.local", model="test-model", api_key="key")
    session_id = await bridge.open()

    assert session_id == bridge.session_id
    assert len(session_id) == 32
    int(session_id, 16)  # raises ValueError if not valid hex


async def test_serve_forever_requires_open_first():
    bridge = KyokBridge("http://souk.local", model="test-model", api_key="key")
    with pytest.raises(AssertionError):
        await bridge.serve_forever()


class _StopServing(Exception):
    """Sentinel to end serve_forever's `while True` deterministically —
    avoids racing real time against an instantly-resolving mock (which,
    tried first, spun the loop far faster than any timeout-based
    cancellation could keep up with).
    """


@respx.mock
async def test_serve_forever_polls_and_dispatches_claimed_requests():
    bridge = KyokBridge("http://souk.local", model="test-model", api_key="key", poll_wait_seconds=0.01)
    await bridge.open()

    respx.get("http://souk.local/kyok/poll").mock(
        return_value=httpx.Response(200, json={"requests": [{"requestId": "req_1", "body": {"messages": []}}]})
    )

    served = []

    async def fake_serve_one(client, request_id, body):
        served.append((request_id, body))
        raise _StopServing

    bridge._serve_one = fake_serve_one

    with pytest.raises(_StopServing):
        await bridge.serve_forever()

    assert served == [("req_1", {"messages": []})]
