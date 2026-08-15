"""Optional Keep Your Own Key bridge: lets a souk-client-sdk caller pay
for a run's LLM usage with their own key instead of leaving that to the
agent provider. Purely additive — a caller that never touches this module
is simply not offering KYOK; `SoukClient.run()` alone is a complete,
ordinary caller either way. See docs/keep-your-own-key.md in the souk
repo for the design, and the gateway repo's docs/server-mode.md for the
wire protocol this speaks.

**Experimental** — see tests/test_kyok_bridge.py for this module's
coverage (souk-client-sdk's only tests today). This bridge still holds
no state that survives a crash: if this process dies mid-run, souk fails
the completions it was holding and the run's provider sees errors, with
no retry/resume path on either end. Matches the same "experimental,
in-memory, single-process" status as its souk-side counterpart.

The transport is one WebSocket to the gateway's `/ws/kyok`, held for the
run's whole duration: souk pushes each `completionRequest` down it, and
this bridge streams the real LLM's chunks back as frames on the same
socket, multiplexed by requestId — concurrent completions just interleave.
The `sessionId` sent in `hello` is the same locally-minted routing key the
old `/kyok/poll` query carried; souk still neither mints nor verifies it.
An answer is only accepted on the socket its request was delivered to, so
a reconnect starts fresh: completions in flight on a dead socket are
failed by souk immediately rather than retried here.

Uses litellm (https://github.com/BerriAI/litellm) to actually call the
real LLM, so this bridge isn't tied to one provider — model strings are
litellm's own ("anthropic/claude-...", "gemini/...", "openai/...", a
custom OpenAI-compatible `api_base`, ...) and its streaming chunks are
already OpenAI-shaped, which is exactly what souk's relay expects: no
translation layer needed here, just forward what litellm already gives us.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import litellm
import websockets

logger = logging.getLogger("souk_client_sdk.kyok_bridge")

WELCOME_TIMEOUT_SECONDS = 10.0


class KyokBridge:
    """One bridge serves one run's worth of KYOK completions. Typical
    use: call `open()` to get a session_id, pass it as
    `metadata={"kyok": {"sessionId": session_id}}` to `SoukClient.run()`,
    and run `serve_forever()` as a background task alongside consuming
    that run's event stream — cancel it once the run finishes.
    """

    def __init__(
        self,
        souk_http_url: str,
        model: str,
        api_key: str,
        *,
        api_base: str | None = None,
        reconnect_delay: float = 2.0,
    ) -> None:
        self.souk_http_url = souk_http_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.api_base = api_base
        self.reconnect_delay = reconnect_delay
        self.session_id: str | None = None

    @property
    def _ws_url(self) -> str:
        scheme, netloc, path, _query, _fragment = urlsplit(self.souk_http_url)
        ws_scheme = "wss" if scheme == "https" else "ws"
        return urlunsplit((ws_scheme, netloc, path.rstrip("/") + "/ws/kyok", "", ""))

    async def open(self) -> str:
        """Mints this bridge's session_id locally — souk never hands one
        out up front: it accepts whichever session_id first shows up and
        whichever run's forwardedProps.kyok names it, so nothing needs
        reserving ahead of the run existing. Call this before starting the
        run so serve_forever() is already connected by the time a provider
        might need it.
        """
        self.session_id = secrets.token_hex(16)
        return self.session_id

    async def serve_forever(self) -> None:
        """Holds the `/ws/kyok` socket and serves every completion souk
        pushes down it — calls the real LLM via litellm using this
        bridge's own api_key, and streams the response back as frames.
        Reconnects on a drop. Runs until cancelled; intended to be wrapped
        in `asyncio.create_task` alongside the run it's serving, not
        awaited to completion (a KYOK bridge has no natural end of its own
        — the run it's serving does).
        """
        assert self.session_id is not None, "call open() before serve_forever()"
        while True:
            try:
                await self._serve_connection()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "kyok bridge connection lost; reconnecting in %.1fs", self.reconnect_delay
                )
            await asyncio.sleep(self.reconnect_delay)

    async def _serve_connection(self) -> None:
        async with websockets.connect(self._ws_url) as ws:
            await ws.send(json.dumps({"type": "hello", "sessionId": self.session_id}))
            welcome = json.loads(await asyncio.wait_for(ws.recv(), WELCOME_TIMEOUT_SECONDS))
            if welcome.get("type") != "welcome":
                raise RuntimeError(f"expected welcome, got {welcome!r}")

            # Single writer: concurrent completions queue frames here
            # rather than interleaving sends on the socket directly.
            outbound: asyncio.Queue = asyncio.Queue()
            writer = asyncio.create_task(self._write_loop(ws, outbound))
            in_flight: set[asyncio.Task] = set()
            try:
                async for raw in ws:
                    frame = json.loads(raw)
                    kind = frame.get("type")
                    if kind == "completionRequest":
                        task = asyncio.create_task(
                            self._serve_one(outbound, frame["requestId"], frame["payload"])
                        )
                        in_flight.add(task)
                        task.add_done_callback(in_flight.discard)
                    elif kind == "error":
                        logger.warning("souk rejected a frame: %s", frame)
                    else:
                        logger.warning("unexpected frame from souk, ignoring: %s", frame)
            finally:
                writer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await writer
                # Answers are only accepted on the socket their request
                # arrived on, and this one is gone — souk is already
                # failing these completions. Stop paying the LLM for
                # output with nowhere to go.
                for task in list(in_flight):
                    task.cancel()

    async def _write_loop(self, ws, outbound: asyncio.Queue) -> None:
        while True:
            await ws.send(json.dumps(await outbound.get()))

    async def _serve_one(self, outbound: asyncio.Queue, request_id: str, body: dict[str, Any]) -> None:
        """One completion: stream the real LLM's chunks back as `chunk`
        frames, then `done` — or one `error` frame if the call fails
        outright, so the waiting provider fails fast instead of timing
        out."""
        try:
            stream = await litellm.acompletion(
                model=self.model,
                api_key=self.api_key,
                api_base=self.api_base,
                messages=body.get("messages", []),
                tools=body.get("tools") or None,
                temperature=body.get("temperature"),
                stream=True,
            )
            async for chunk in stream:
                outbound.put_nowait(
                    {"type": "chunk", "requestId": request_id, "data": _to_chunk_dict(chunk)}
                )
            outbound.put_nowait({"type": "done", "requestId": request_id})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("kyok bridge: LLM call failed for request_id=%s", request_id)
            outbound.put_nowait({"type": "error", "requestId": request_id, "message": str(e)})


def _to_chunk_dict(chunk: Any) -> dict:
    """One litellm streaming chunk as the plain dict souk relays — litellm
    returns pydantic-ish objects whose serialization surface varies by
    version, hence the three paths."""
    if hasattr(chunk, "model_dump"):
        return chunk.model_dump(mode="json")
    if hasattr(chunk, "dict"):
        return chunk.dict()
    return dict(chunk)
