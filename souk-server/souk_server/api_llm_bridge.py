"""KYOK HTTP surface: routes only.

What KYOK *means* — the two-part authorization, the relay between a provider
and whoever is paying for its inference, reassembling streamed chunks — lives
in souk/protocols/kyok.py, in core. This file lifts headers off requests,
frames results as SSE or JSON, and maps `KyokRejected` onto its status.

The shape mirrors souk_agent_sdk.client's idle/active split, so neither side
holds a connection open for the lifetime of a run:

- `GET /kyok/poll`: a caller's bridge long-polls this, exactly like
  PollForWork — empty most of the time, returning almost instantly once a
  provider actually calls into the bridge, never itself a held-open
  connection.
- `POST /kyok/v1/chat/completions`: what a provider's OpenAI-compatible model
  client actually calls — it looks exactly like a real OpenAI-compatible
  host. Queues the request for the caller's bridge to notice via the poll
  above, then blocks *this HTTP call* while relaying the answer back.
- `POST /kyok/respond/{request_id}`: the caller's bridge streams the real
  LLM's chunks back through here — the only genuinely held-open connection
  either side has, and only while tokens are actually moving.

See docs/keep-your-own-key.md for the full picture.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from souk.core import Souk
from souk_server.deps import get_souk
from souk.errors import KyokRejected
from souk.protocols.kyok import KyokAdapter

logger = logging.getLogger("souk.api_llm_bridge")

router = APIRouter()


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[len("Bearer ") :] if header.startswith("Bearer ") else header


@router.get("/kyok/poll")
async def poll(
    session_id: str = Query(..., alias="sessionId"),
    wait_seconds: float = Query(25.0, alias="waitSeconds"),
    souk: Souk = Depends(get_souk),
) -> dict:
    result = await KyokAdapter(souk).poll(session_id, wait_seconds)
    return {"requests": [result] if result else []}


@router.post("/kyok/v1/chat/completions", response_model=None)
async def chat_completions(
    request: Request, souk: Souk = Depends(get_souk)
) -> StreamingResponse | JSONResponse:
    relay = await KyokAdapter(souk).complete(
        _bearer(request),
        await request.body(),
        timestamp=request.headers.get("x-souk-kyok-timestamp", ""),
        signature=request.headers.get("x-souk-kyok-signature", ""),
    )
    if not relay.stream_requested:
        return JSONResponse(await relay.collapsed())

    async def sse():
        # A rejection here surfaces mid-stream: the response has already
        # started, so there is no status left to change and the caller sees
        # the stream simply end.
        try:
            async for data in relay.encode():
                yield f"data: {data}\n\n"
        except KyokRejected as e:
            logger.warning("kyok completion %s ended early: %s", relay.request_id, e)

    return StreamingResponse(sse(), media_type="text/event-stream")


@router.post("/kyok/respond/{request_id}")
async def respond(request_id: str, request: Request, souk: Souk = Depends(get_souk)) -> dict:
    """The caller's bridge streams the real LLM's response back as
    newline-delimited JSON, read incrementally off the request body rather
    than buffered whole. The connection closing (EOF) is what ends the relay.
    """
    await KyokAdapter(souk).respond(request_id, request.stream())
    return {"ok": True}
