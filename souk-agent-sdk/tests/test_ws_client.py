"""SoukProvider's WebSocket transport (souk_agent_sdk.client) against a
stub gateway speaking the /ws/provider frame protocol — the table in the
gateway repo's docs/server-mode.md, which that repo authors and this SDK
implements. The stub is the server half of one socket: it answers hello
with welcome, pushes what a test tells it to, and records every frame the
provider sends back.

registration is HTTP and separately covered; these tests inject the
session token and agent_id map directly and drive `_run_connection`, the
transport under test.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import pytest
import websockets

from souk_agent_sdk.client import AgentHandle, SoukProvider

RECEIVE_TIMEOUT = 2.0


class StubGateway:
    def __init__(self) -> None:
        self.hello: dict | None = None
        self.auth_header: str | None = None
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
        self.auth_header = ws.request.headers.get("Authorization")
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


def _provider(port: int, handles: list[AgentHandle], **kwargs: Any) -> SoukProvider:
    provider = SoukProvider(f"http://127.0.0.1:{port}", handles, **kwargs)
    # What register() would have set — HTTP registration is not under test.
    provider._session_token = "tok-123"
    provider._handle_by_id = {f"agent_{h.name}": h for h in handles}
    return provider


async def _echo_run_stream(run_input: dict) -> Any:
    yield {"type": "RUN_STARTED", "runId": run_input["runId"]}
    yield {"type": "RUN_FINISHED", "runId": run_input["runId"]}


async def test_hello_rides_both_tracks_and_declares_the_budget():
    async with StubGateway() as gateway:
        provider = _provider(
            gateway.port,
            [AgentHandle(name="echo", run_stream=_echo_run_stream)],
            max_concurrent_runs=2,
        )
        conn = asyncio.create_task(provider._run_connection())
        try:
            async with asyncio.timeout(RECEIVE_TIMEOUT):
                await gateway.connected.wait()
            assert gateway.auth_header == "Bearer tok-123"
            assert gateway.hello == {
                "type": "hello",
                "token": "tok-123",
                "agentIds": ["agent_echo"],
                "maxClaim": 2,
            }
        finally:
            conn.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await conn


async def test_a_pushed_run_comes_back_as_events_then_finish():
    async with StubGateway() as gateway:
        provider = _provider(gateway.port, [AgentHandle(name="echo", run_stream=_echo_run_stream)])
        conn = asyncio.create_task(provider._run_connection())
        try:
            async with asyncio.timeout(RECEIVE_TIMEOUT):
                await gateway.connected.wait()
            await gateway.push(
                {
                    "type": "run",
                    "runId": "r1",
                    "threadId": "t1",
                    "agentId": "agent_echo",
                    "input": {"runId": "r1", "threadId": "t1", "messages": []},
                }
            )
            frames = [await gateway.next_frame() for _ in range(3)]
            assert [f["type"] for f in frames] == ["event", "event", "finish"]
            assert frames[0]["event"]["type"] == "RUN_STARTED"
            assert frames[1]["event"]["type"] == "RUN_FINISHED"
            assert all(f["runId"] == "r1" for f in frames)
        finally:
            conn.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await conn


async def test_a_cancel_interrupts_the_run_and_finish_still_goes_out():
    started = asyncio.Event()

    async def stuck_run_stream(run_input: dict) -> Any:
        yield {"type": "RUN_STARTED", "runId": run_input["runId"]}
        started.set()
        await asyncio.sleep(3600)  # a run that would never end on its own

    async with StubGateway() as gateway:
        provider = _provider(gateway.port, [AgentHandle(name="stuck", run_stream=stuck_run_stream)])
        conn = asyncio.create_task(provider._run_connection())
        try:
            async with asyncio.timeout(RECEIVE_TIMEOUT):
                await gateway.connected.wait()
            await gateway.push(
                {"type": "run", "runId": "r1", "threadId": "t1", "agentId": "agent_stuck", "input": {"runId": "r1"}}
            )
            assert (await gateway.next_frame())["type"] == "event"
            async with asyncio.timeout(RECEIVE_TIMEOUT):
                await started.wait()

            await gateway.push({"type": "cancel", "runId": "r1"})
            # Complying is the worker's choice, and this worker complies:
            # the run's current await is interrupted, and finish — the
            # last word souk decides the outcome from — still goes out.
            frame = await gateway.next_frame()
            assert frame == {"type": "finish", "runId": "r1"}
        finally:
            conn.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await conn


async def test_a_run_for_an_unknown_agent_is_dropped_without_taking_the_socket_down():
    async with StubGateway() as gateway:
        provider = _provider(gateway.port, [AgentHandle(name="echo", run_stream=_echo_run_stream)])
        conn = asyncio.create_task(provider._run_connection())
        try:
            async with asyncio.timeout(RECEIVE_TIMEOUT):
                await gateway.connected.wait()
            await gateway.push(
                {"type": "run", "runId": "r_alien", "threadId": "t1", "agentId": "not_ours", "input": {}}
            )
            # Still serving: a well-formed run for an agent we do host
            # flows normally after the alien one was dropped.
            await gateway.push(
                {"type": "run", "runId": "r2", "threadId": "t1", "agentId": "agent_echo", "input": {"runId": "r2"}}
            )
            frames = [await gateway.next_frame() for _ in range(3)]
            assert all(f["runId"] == "r2" for f in frames)
        finally:
            conn.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await conn


async def test_frames_of_a_run_cut_short_by_a_drop_go_out_on_the_next_socket():
    """The outbound queue is deliberately not per-connection: a run is
    addressed by runId, not by the socket it arrived on, so what a dead
    socket failed to carry goes out on the next one — the SDK half of the
    reconnect-and-finish property the gateway pins."""
    release = asyncio.Event()

    async def two_phase_run_stream(run_input: dict) -> Any:
        yield {"type": "RUN_STARTED", "runId": run_input["runId"]}
        await release.wait()
        yield {"type": "RUN_FINISHED", "runId": run_input["runId"]}

    async with StubGateway() as gateway:
        provider = _provider(
            gateway.port, [AgentHandle(name="twophase", run_stream=two_phase_run_stream)]
        )
        conn = asyncio.create_task(provider._run_connection())
        async with asyncio.timeout(RECEIVE_TIMEOUT):
            await gateway.connected.wait()
        await gateway.push(
            {"type": "run", "runId": "r1", "threadId": "t1", "agentId": "agent_twophase", "input": {"runId": "r1"}}
        )
        assert (await gateway.next_frame())["type"] == "event"

        # The socket drops mid-run. _run_connection's teardown cancels the
        # run; its finish frame lands on the connection-independent queue.
        await gateway._conn.close()
        with contextlib.suppress(Exception):
            async with asyncio.timeout(RECEIVE_TIMEOUT):
                await conn
        gateway.connected.clear()

        # A fresh connection — run_forever would do exactly this — flushes
        # what the dead one still owed.
        conn2 = asyncio.create_task(provider._run_connection())
        try:
            frame = await gateway.next_frame()
            assert frame == {"type": "finish", "runId": "r1"}
        finally:
            conn2.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await conn2
