"""Covers KyokBridge — the caller-side half of KYOK (Keep Your Own Key).
See souk_client_sdk/kyok_bridge.py's own docstring for the transport
(one WebSocket to /ws/kyok) and docs/keep-your-own-key.md (in the souk
repo) for the design.

Uses a stub /ws/kyok server speaking the gateway's frame protocol (no
real souk instance needed), and monkeypatches litellm.acompletion
directly rather than adding another mocking layer for it — litellm is
already a runtime dependency here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import litellm
import pytest
import websockets

from souk_client_sdk.kyok_bridge import KyokBridge, _to_chunk_dict

RECEIVE_TIMEOUT = 2.0


# --- _to_chunk_dict: all three normalization paths -------------------------


class _ModelDumpChunk:
    def model_dump(self, mode: str = "python") -> dict:
        return {"via": "model_dump", "mode": mode}


class _DictMethodChunk:
    def dict(self) -> dict:
        return {"via": "dict_method"}


def test_to_chunk_dict_uses_model_dump_when_available():
    assert _to_chunk_dict(_ModelDumpChunk()) == {"via": "model_dump", "mode": "json"}


def test_to_chunk_dict_falls_back_to_dict_method():
    assert _to_chunk_dict(_DictMethodChunk()) == {"via": "dict_method"}


def test_to_chunk_dict_falls_back_to_plain_dict_conversion():
    assert _to_chunk_dict({"via": "plain_dict"}) == {"via": "plain_dict"}


# --- open / preconditions --------------------------------------------------


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


# --- the socket ------------------------------------------------------------


class StubGateway:
    """The server half of one /ws/kyok socket: answers hello with welcome,
    pushes what a test tells it to, records every frame the bridge sends."""

    def __init__(self) -> None:
        self.hello: dict | None = None
        self.frames: asyncio.Queue = asyncio.Queue()
        self.connected = asyncio.Event()
        self._conn = None

    async def __aenter__(self) -> "StubGateway":
        self._server = await websockets.serve(self._handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def _handler(self, ws) -> None:
        self.hello = json.loads(await ws.recv())
        await ws.send(json.dumps({"type": "welcome"}))
        self._conn = ws
        self.connected.set()
        async for raw in ws:
            self.frames.put_nowait(json.loads(raw))

    async def push(self, frame: dict) -> None:
        await self._conn.send(json.dumps(frame))

    async def next_frame(self) -> dict:
        async with asyncio.timeout(RECEIVE_TIMEOUT):
            return await self.frames.get()


async def _connected_bridge(gateway: StubGateway, **kwargs: Any):
    bridge = KyokBridge(f"http://127.0.0.1:{gateway.port}", model="test-model", api_key="key", **kwargs)
    await bridge.open()
    task = asyncio.create_task(bridge.serve_forever())
    async with asyncio.timeout(RECEIVE_TIMEOUT):
        await gateway.connected.wait()
    return bridge, task


async def test_hello_carries_the_session_id():
    async with StubGateway() as gateway:
        bridge, task = await _connected_bridge(gateway)
        try:
            assert gateway.hello == {"type": "hello", "sessionId": bridge.session_id}
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def test_a_completion_request_streams_back_as_chunks_then_done(monkeypatch):
    captured: dict = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)

        async def gen():
            yield {"choices": [{"delta": {"role": "assistant", "content": "hi"}}]}
            yield {"choices": [{"delta": {"content": " there"}}]}

        return gen()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    async with StubGateway() as gateway:
        _bridge, task = await _connected_bridge(gateway, api_base="http://llm.local")
        try:
            body = {
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "function"}],
                "temperature": 0.5,
            }
            await gateway.push({"type": "completionRequest", "requestId": "req_1", "payload": body})

            frames = [await gateway.next_frame() for _ in range(3)]
            assert [f["type"] for f in frames] == ["chunk", "chunk", "done"]
            assert all(f["requestId"] == "req_1" for f in frames)
            assert frames[0]["data"]["choices"][0]["delta"]["content"] == "hi"

            # The provider's whole request body reached litellm, on this
            # bridge's own key.
            assert captured["model"] == "test-model"
            assert captured["api_key"] == "key"
            assert captured["api_base"] == "http://llm.local"
            assert captured["messages"] == body["messages"]
            assert captured["tools"] == body["tools"]
            assert captured["temperature"] == 0.5
            assert captured["stream"] is True
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def test_an_llm_failure_becomes_one_error_frame(monkeypatch):
    async def fake_acompletion(**kwargs):
        raise RuntimeError("upstream boom")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    async with StubGateway() as gateway:
        _bridge, task = await _connected_bridge(gateway)
        try:
            await gateway.push(
                {"type": "completionRequest", "requestId": "req_1", "payload": {"messages": []}}
            )
            frame = await gateway.next_frame()
            assert frame == {"type": "error", "requestId": "req_1", "message": "upstream boom"}
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def test_concurrent_completions_multiplex_on_one_socket(monkeypatch):
    """Two requests in flight at once; their chunks interleave by
    requestId — the property that made the socket strictly better than
    poll_one's one-per-cycle handover."""
    release = asyncio.Event()

    async def fake_acompletion(**kwargs):
        prompt = kwargs["messages"][0]["content"]

        async def gen():
            if prompt == "slow":
                await release.wait()
            yield {"choices": [{"delta": {"content": f"re: {prompt}"}}]}

        return gen()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    async with StubGateway() as gateway:
        _bridge, task = await _connected_bridge(gateway)
        try:
            await gateway.push(
                {
                    "type": "completionRequest",
                    "requestId": "req_slow",
                    "payload": {"messages": [{"role": "user", "content": "slow"}]},
                }
            )
            await gateway.push(
                {
                    "type": "completionRequest",
                    "requestId": "req_fast",
                    "payload": {"messages": [{"role": "user", "content": "fast"}]},
                }
            )
            # The fast one answers while the slow one is still held open.
            first = await gateway.next_frame()
            assert first["requestId"] == "req_fast"
            release.set()
            rest = [await gateway.next_frame() for _ in range(3)]
            assert {"req_fast", "req_slow"} == {f["requestId"] for f in [first, *rest]}
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
