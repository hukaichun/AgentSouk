"""Optional Keep Your Own Key bridge: lets a souk-client-sdk caller pay
for a run's LLM usage with their own key instead of leaving that to the
agent provider. Purely additive — a caller that never touches this module
is simply not offering KYOK; `SoukClient.run()` alone is a complete,
ordinary caller either way. See docs/keep-your-own-key.md in the souk
repo for the full picture and wire protocol.

Uses litellm (https://github.com/BerriAI/litellm) to actually call the
real LLM, so this bridge isn't tied to one provider — model strings are
litellm's own ("anthropic/claude-...", "gemini/...", "openai/...", a
custom OpenAI-compatible `api_base`, ...) and its streaming chunks are
already OpenAI-shaped, which is exactly the wire format souk's
/kyok/v1/chat/completions <-> /kyok/respond relay expects: no translation
layer needed here, just forward what litellm already gives us.
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import AsyncIterator
from typing import Any

import httpx
import litellm

logger = logging.getLogger("souk_client_sdk.kyok_bridge")


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
        poll_wait_seconds: float = 25.0,
    ) -> None:
        self.souk_http_url = souk_http_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.api_base = api_base
        self.poll_wait_seconds = poll_wait_seconds
        self.session_id: str | None = None

    async def open(self) -> str:
        """Mints this bridge's session_id locally — souk never hands one
        out up front (see souk.api_llm_bridge): it accepts whichever
        session_id first shows up polling and whichever run's
        forwardedProps.kyok names it, so nothing needs reserving ahead of
        the run existing. Call this before starting the run so
        serve_forever() is already polling by the time a provider might
        need it.
        """
        self.session_id = secrets.token_hex(16)
        return self.session_id

    async def serve_forever(self) -> None:
        """Long-polls /kyok/poll for this session's queued completions
        and serves each one — calls the real LLM via litellm using this
        bridge's own api_key, and streams the response back through
        /kyok/respond. Runs until cancelled; intended to be wrapped in
        `asyncio.create_task` alongside the run it's serving, not awaited
        to completion (a KYOK bridge has no natural end of its own — the
        run it's serving does).
        """
        assert self.session_id is not None, "call open() before serve_forever()"
        async with httpx.AsyncClient(timeout=self.poll_wait_seconds + 10.0) as client:
            while True:
                response = await client.get(
                    f"{self.souk_http_url}/kyok/poll",
                    params={"sessionId": self.session_id, "waitSeconds": self.poll_wait_seconds},
                )
                response.raise_for_status()
                for item in response.json().get("requests", []):
                    await self._serve_one(client, item["requestId"], item["body"])

    async def _serve_one(self, client: httpx.AsyncClient, request_id: str, body: dict[str, Any]) -> None:
        try:
            await client.post(
                f"{self.souk_http_url}/kyok/respond/{request_id}",
                content=self._call_llm(request_id, body),
            )
        except Exception:
            logger.exception("kyok bridge: failed to relay response for request_id=%s", request_id)

    async def _call_llm(self, request_id: str, body: dict[str, Any]) -> AsyncIterator[bytes]:
        """Yields newline-delimited JSON chunks — souk's
        /kyok/respond wire format — one per litellm streaming chunk
        (already OpenAI chat.completion.chunk-shaped), or one
        `{"error": ...}` line if the call fails outright.
        """
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
                yield _to_json_line(chunk)
        except Exception as e:
            logger.exception("kyok bridge: LLM call failed for request_id=%s", request_id)
            yield json.dumps({"error": str(e)}).encode() + b"\n"


def _to_json_line(chunk: Any) -> bytes:
    if hasattr(chunk, "model_dump"):
        data = chunk.model_dump(mode="json")
    elif hasattr(chunk, "dict"):
        data = chunk.dict()
    else:
        data = dict(chunk)
    return json.dumps(data).encode() + b"\n"
