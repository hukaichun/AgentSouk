"""KYOK translation: relaying a provider's LLM call to whoever is paying.

Extracted from what used to be protocols.kyok, minus everything about HTTP.
KYOK is structurally a second broker — a provider submits work (a completion),
the caller's own bridge claims it and streams back the answer — so it splits
the same way runs do: the mechanism and its checks live in core, the three
`/kyok/*` endpoints are serving.

What this deliberately holds rather than leaving to a route: the two-part
authorization. A KYOK token proves souk minted it and names a run; it does
not prove who is presenting it. The call-time signature is the other half,
and getting either wrong means someone else spending the caller's real LLM
budget — not the kind of check to reimplement per transport.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from souk import repo
from souk.errors import KyokRejected
from souk.identity import is_timestamp_fresh, verify_signature
from souk.kyok import COMPLETION_DONE, KyokToken, verify_kyok_token

if TYPE_CHECKING:
    from souk.core import Souk

logger = logging.getLogger("souk.protocols.kyok")

# How long a provider's completion waits, queued, for the caller's bridge to
# notice it before souk gives up — the KYOK counterpart of a run's
# queued_timeout_seconds, kept separate because a completion is expected to be
# claimed far faster: the bridge is meant to be polling continuously for the
# run's whole duration, not discovering work cold.
CLAIM_TIMEOUT_SECONDS = 30.0


@dataclass
class CompletionRelay:
    """A submitted completion, and the answer coming back for it."""

    request_id: str
    stream_requested: bool
    chunks: AsyncIterator[dict[str, Any]]

    async def collapsed(self) -> dict[str, Any]:
        """Every chunk reassembled into one non-streaming response, for a
        provider that didn't ask to stream."""
        return collapse_stream([chunk async for chunk in self.chunks])

    async def encode(self) -> AsyncIterator[str]:
        """The chunks as OpenAI-style SSE payloads, terminator included."""
        async for chunk in self.chunks:
            yield json.dumps(chunk)
            if isinstance(chunk, dict) and set(chunk) == {"error"}:
                return
        yield "[DONE]"


class KyokAdapter:
    """KYOK semantics over a Souk."""

    def __init__(self, souk: "Souk") -> None:
        self._souk = souk

    async def poll(self, session_id: str, wait_seconds: float) -> dict[str, Any] | None:
        """A caller's bridge asking whether any completion is waiting for it."""
        return await self._souk.kyok_bridge.poll_one(session_id, wait_seconds)

    async def complete(
        self,
        bearer: str,
        body: bytes,
        *,
        timestamp: str,
        signature: str,
    ) -> CompletionRelay:
        """A provider's OpenAI-shaped call, authorized and relayed.

        Three checks, all here rather than in a route:

        1. the KYOK token is souk's own and unexpired,
        2. the run it names is *still genuinely in flight* for that agent —
           which caps a leaked token's usable window to the life of that run,
           rather than leaving the token's own hour-long TTL as the only thing
           between a finished run and someone spending the caller's budget,
        3. the call is signed by the agent's own key, over this
           exact token + timestamp + body, so a signature can't be replayed
           onto another request.
        """
        token = verify_kyok_token(bearer, self._souk.settings.token_signing_secret)
        if token is None:
            raise KyokRejected("invalid or expired KYOK token", status=401)

        run = self._souk.broker.get(token.run_id)
        if run is None or run.cancel_requested or run.agent != token.agent:
            raise KyokRejected("run is not currently active for this token", status=403)

        await self._verify_caller(token, bearer, body, timestamp, signature)

        payload = json.loads(body)
        request_id, queue = self._souk.kyok_bridge.submit(token.session_id, payload)
        return CompletionRelay(
            request_id=request_id,
            stream_requested=bool(payload.get("stream")),
            chunks=self._drain(request_id, queue),
        )

    async def respond(self, request_id: str, lines: AsyncIterator[bytes]) -> None:
        """The caller's bridge streaming the real LLM's answer back, as
        newline-delimited JSON. Consumed incrementally so a long completion
        never sits whole in memory on either side."""
        pending = self._souk.kyok_bridge.get(request_id)
        if pending is None:
            raise KyokRejected(f"no pending KYOK completion '{request_id}'", status=404)

        buffer = b""
        async for piece in lines:
            buffer += piece
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("kyok respond %s: dropping malformed line", request_id)
                    continue
                if isinstance(message, dict) and message.get("error"):
                    pending.response_queue.put_nowait({"error": message["error"]})
                    break
                pending.response_queue.put_nowait(message)
        pending.response_queue.put_nowait(COMPLETION_DONE)

    async def _verify_caller(
        self, token: KyokToken, bearer: str, body: bytes, timestamp: str, signature: str
    ) -> None:
        """The half the token can't cover: souk_agent_sdk.KyokSigningAuth
        signs every call with the calling provider's registration identity, so
        the key is the token's own — souk being the
        one source of truth for that — and checks the signature really came
        from it. Reuses souk.identity's freshness window rather than a second
        constant; no reason KYOK's should ever diverge.
        """
        if not timestamp or not signature:
            raise KyokRejected("missing KYOK call-time signature", status=401)
        try:
            fresh = is_timestamp_fresh(int(timestamp))
        except ValueError as e:
            raise KyokRejected("malformed KYOK signature timestamp", status=401) from e
        if not fresh:
            raise KyokRejected("KYOK signature timestamp is stale", status=401)

        # Still asked, because a de-listed agent must not keep spending a
        # caller's key — but no longer asked in order to *learn* the key.
        # The token carries it: an agent is `(provider_key, name)`, souk
        # signed the token itself, and there used to be a whole lookup here
        # (`repo.get_agent_public_key`) whose only job was turning an id back
        # into the identity it always stood for.
        async with self._souk.session() as session:
            registered = await repo.get_agent(session, token.agent)
        if registered is None:
            raise KyokRejected(f"agent '{token.agent}' is not registered", status=403)

        payload = f"{bearer}:{timestamp}:{hashlib.sha256(body).hexdigest()}".encode()
        if not verify_signature(token.agent.provider_key, signature, payload):
            raise KyokRejected("KYOK call-time signature verification failed", status=401)

    async def _drain(self, request_id: str, queue: asyncio.Queue) -> AsyncIterator[dict[str, Any]]:
        try:
            while True:
                item = await asyncio.wait_for(queue.get(), timeout=CLAIM_TIMEOUT_SECONDS)
                if item is COMPLETION_DONE:
                    return
                yield item
        except asyncio.TimeoutError as e:
            raise KyokRejected(
                "no KYOK bridge claimed this completion in time", status=502
            ) from e
        finally:
            self._souk.kyok_bridge.forget(request_id)


def collapse_stream(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """Reassembles OpenAI-style streaming chunks (chat.completion.chunk,
    delta-shaped) into one non-streaming chat.completion response, for a
    provider that called /kyok/v1/chat/completions with `stream` unset or
    false. The caller's bridge always sends streaming-shaped chunks (see
    docs/keep-your-own-key.md) regardless of what the provider asked for
    — this is where a non-streaming caller gets that collapsed back down
    instead of needing its own merge logic.
    """
    if not chunks:
        return {
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}
            ]
        }

    first = chunks[0]
    content_by_index: dict[int, str] = {}
    finish_reason_by_index: dict[int, str | None] = {}
    role = "assistant"
    for chunk in chunks:
        for choice in chunk.get("choices", []):
            index = choice.get("index", 0)
            delta = choice.get("delta", {})
            if "role" in delta:
                role = delta["role"]
            content_by_index[index] = content_by_index.get(index, "") + delta.get("content", "")
            if choice.get("finish_reason"):
                finish_reason_by_index[index] = choice["finish_reason"]

    return {
        "id": first.get("id"),
        "object": "chat.completion",
        "created": first.get("created"),
        "model": first.get("model"),
        "choices": [
            {
                "index": index,
                "message": {"role": role, "content": content},
                "finish_reason": finish_reason_by_index.get(index),
            }
            for index, content in sorted(content_by_index.items())
        ],
    }
