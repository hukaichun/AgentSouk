"""souk's OpenAI-compatible LLM bridge — see docs/keep-your-own-key.md for
the full picture.

Mirrors souk_agent_sdk.client's idle/active shape for providers (long-poll
while idle, open a connection only once there's real work — see that
module's docstring) rather than asking a KYOK caller to hold anything open
for the lifetime of a run:

- `GET /kyok/poll`: a caller's bridge long-polls this, exactly like
  PollForWork (souk.grpc_server.SoukAgentGateway.PollForWork) — empty
  most of the time, returning almost instantly once a provider actually
  calls into the bridge, never itself a source of a held-open connection.
- `POST /kyok/v1/chat/completions`: what a provider's OpenAI-compatible
  model client actually calls — looks exactly like a real OpenAI-compatible
  host. Queues the request for the caller's bridge to notice via the poll
  above, then blocks (this HTTP call, not any long-lived connection)
  relaying the caller's response back as it arrives.
- `POST /kyok/respond/{request_id}`: the caller's bridge streams the real
  LLM's response chunks back through this — one connection, held open
  only for the duration of this one completion, closed the instant it's
  done. This is the only "active" connection either side ever holds, and
  only while genuinely relaying tokens.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from souk import repo
from souk.broker import broker
from souk.db import get_session
from souk.identity import is_timestamp_fresh, verify_signature
from souk.ids import new_id
from souk.kyok import KyokToken, verify_kyok_token

logger = logging.getLogger("souk.api_llm_bridge")

router = APIRouter()

# Sentinel put on a completion's response_queue once its bridge finishes
# sending chunks (or the /kyok/respond connection drops) — mirrors
# broker.END_OF_STREAM.
_DONE = object()

# How long a provider's completion request waits, queued, for the
# caller's bridge to notice it via /kyok/poll before souk gives up —
# same purpose as souk.config.settings.queued_timeout_seconds for
# ordinary runs, kept local rather than shared since a KYOK completion is
# expected to be claimed far faster than a run (the caller's bridge is
# meant to be polling continuously for the run's whole duration, not
# discovering work cold).
CLAIM_TIMEOUT_SECONDS = 30.0


@dataclass
class PendingCompletion:
    session_id: str
    body: dict[str, Any]
    response_queue: asyncio.Queue[Any] = field(default_factory=asyncio.Queue)
    claimed: bool = False


class KyokBridge:
    """In-memory registry connecting a provider's queued completion
    requests to whichever caller session they belong to — same
    single-process assumption as broker.RunBroker (see its module
    docstring), for the same reason: none of this needs to survive a
    restart.
    """

    def __init__(self) -> None:
        self._pending_by_session: dict[str, deque[str]] = defaultdict(deque)
        self._requests: dict[str, PendingCompletion] = {}
        self._wake_subscribers: dict[str, set[asyncio.Event]] = defaultdict(set)

    def submit(self, session_id: str, body: dict[str, Any]) -> tuple[str, asyncio.Queue[Any]]:
        request_id = new_id("kyokreq")
        pending = PendingCompletion(session_id=session_id, body=body)
        self._requests[request_id] = pending
        self._pending_by_session[session_id].append(request_id)
        for event in self._wake_subscribers.get(session_id, ()):
            event.set()
        return request_id, pending.response_queue

    async def poll_one(self, session_id: str, wait_seconds: float) -> dict[str, Any] | None:
        """Returns `{"requestId": ..., "body": ...}` for the next
        completion queued for `session_id`, waiting up to `wait_seconds`
        if none is queued yet (0 means return immediately either way).
        None if nothing showed up within the wait.
        """
        queue = self._pending_by_session[session_id]
        if not queue and wait_seconds > 0:
            event = asyncio.Event()
            self._wake_subscribers[session_id].add(event)
            try:
                await asyncio.wait_for(event.wait(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                pass
            finally:
                self._wake_subscribers[session_id].discard(event)
        if not queue:
            return None
        request_id = queue.popleft()
        pending = self._requests.get(request_id)
        if pending is None:
            return None
        pending.claimed = True
        return {"requestId": request_id, "body": pending.body}

    def get(self, request_id: str) -> PendingCompletion | None:
        return self._requests.get(request_id)

    def forget(self, request_id: str) -> None:
        self._requests.pop(request_id, None)


bridge = KyokBridge()


@router.get("/kyok/poll")
async def poll(session_id: str = Query(..., alias="sessionId"), wait_seconds: float = Query(25.0, alias="waitSeconds")) -> dict:
    result = await bridge.poll_one(session_id, wait_seconds)
    return {"requests": [result] if result else []}


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    return header[len("Bearer "):]


async def _verify_caller_identity(
    request: Request, token: KyokToken, raw_body: bytes, session: AsyncSession
) -> None:
    """The KYOK token alone only proves souk minted it, not who's
    presenting it — see souk.kyok's module docstring. This is the other
    half: souk_agent_sdk.KyokSigningAuth signs every completion call with
    the calling provider's own registration identity, so this looks up
    that agent_id's actual public_key (souk is the one source of truth
    for which key legitimately owns agent_id, same as everywhere else in
    this system — see souk.identity) and verifies the signature really
    was produced by it, over this exact token+timestamp+body — not some
    other request's signature replayed here. Reuses souk.identity's own
    freshness window (SIGNATURE_FRESHNESS_WINDOW_SECONDS) rather than a
    second constant — no reason for KYOK's window to ever diverge from
    every other signed request in this system.
    """
    timestamp = request.headers.get("x-souk-kyok-timestamp", "")
    signature = request.headers.get("x-souk-kyok-signature", "")
    if not timestamp or not signature:
        raise HTTPException(status_code=401, detail="missing KYOK call-time signature")
    try:
        if not is_timestamp_fresh(int(timestamp)):
            raise HTTPException(status_code=401, detail="KYOK signature timestamp is stale")
    except ValueError as e:
        raise HTTPException(status_code=401, detail="malformed KYOK signature timestamp") from e

    public_key = await repo.get_agent_public_key(session, token.agent_id)
    if public_key is None:
        raise HTTPException(status_code=403, detail=f"agent '{token.agent_id}' is not registered")

    bearer = _bearer_token(request)
    body_hash = hashlib.sha256(raw_body).hexdigest()
    payload = f"{bearer}:{timestamp}:{body_hash}".encode()
    if not verify_signature(public_key, signature, payload):
        raise HTTPException(status_code=401, detail="KYOK call-time signature verification failed")


@router.post("/kyok/v1/chat/completions", response_model=None)
async def chat_completions(request: Request, session: AsyncSession = Depends(get_session)) -> StreamingResponse | JSONResponse:
    token = verify_kyok_token(_bearer_token(request))
    if token is None:
        raise HTTPException(status_code=401, detail="invalid or expired KYOK token")

    run = broker.get(token.run_id)
    if run is None or run.cancelled or run.agent_id != token.agent_id:
        # Either this run has already finished/been forgotten (broker.
        # RunBroker.forget — souk's pipeline task calls this the moment a
        # run terminates, see broker.py's _pipeline), was cancelled, or
        # — the case this exists for even though today it can never
        # actually differ, since a run's agent_id is fixed for its whole
        # life from repo.create_run onward — the token's agent_id
        # doesn't match who souk currently has running run_id. Rejecting
        # here caps a leaked/replayed token's usable window to "while the
        # run it names is still genuinely in flight", instead of the
        # token's own KYOK_TOKEN_TTL_SECONDS (souk.kyok) being the only
        # thing standing between a finished run and an hour of someone
        # still being able to spend the caller's real LLM budget under
        # its name.
        raise HTTPException(status_code=403, detail="run is not currently active for this token")

    raw_body = await request.body()
    await _verify_caller_identity(request, token, raw_body, session)

    body = json.loads(raw_body)
    stream_requested = bool(body.get("stream"))
    request_id, response_queue = bridge.submit(token.session_id, body)

    async def drain():
        try:
            while True:
                item = await asyncio.wait_for(response_queue.get(), timeout=CLAIM_TIMEOUT_SECONDS)
                if item is _DONE:
                    return
                yield item
        except asyncio.TimeoutError as e:
            raise HTTPException(
                status_code=502, detail="no KYOK bridge claimed this completion in time"
            ) from e
        finally:
            bridge.forget(request_id)

    if stream_requested:

        async def sse():
            async for item in drain():
                if isinstance(item, dict) and "error" in item and set(item) == {"error"}:
                    yield f"data: {json.dumps(item)}\n\n"
                    return
                yield f"data: {json.dumps(item)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(sse(), media_type="text/event-stream")

    chunks = [item async for item in drain()]
    return JSONResponse(_collapse_stream(chunks))


@router.post("/kyok/respond/{request_id}")
async def respond(request_id: str, request: Request) -> dict:
    """The caller's bridge streams the real LLM's response back here as
    newline-delimited JSON chunks (one OpenAI-shaped streaming chunk per
    line) — read incrementally off the request body as they arrive
    (`request.stream()`), not buffered whole, so a long completion
    doesn't sit fully in memory on either side before any of it reaches
    the waiting provider. The connection closes the moment the bridge is
    done sending (EOF), which is also the moment souk stops relaying to
    the provider — see chat_completions' drain().
    """
    pending = bridge.get(request_id)
    if pending is None:
        raise HTTPException(status_code=404, detail=f"no pending KYOK completion '{request_id}'")

    buffer = b""
    async for piece in request.stream():
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
    pending.response_queue.put_nowait(_DONE)
    return {"ok": True}


def _collapse_stream(chunks: list[dict[str, Any]]) -> dict[str, Any]:
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
