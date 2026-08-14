"""A2A HTTP surface: routes only.

What A2A *means* — Task.id being souk's run_id, contextId being thread_id,
what tasks/send does when a session already has a live run — lives in
souk/protocols/a2a.py, in core. This file parses requests, frames results as
JSON or SSE, and maps souk.errors onto status codes.

Agent Cards are served under a per-agent path prefix rather than at the
origin root — a deliberate deviation from A2A's single-agent-per-origin
assumption, since one souk fronts many agents at one origin.

Two ways to address an agent:
- `/a2a/id/{agent_id}/...` — the canonical, always-unambiguous route keyed
  by souk's own assigned id (see souk/schema.py's `agents.agent_id`).
- `/a2a/{name}/...` — the legacy, human-readable route, kept working for
  convenience: resolves transparently as long as exactly one currently-
  listed agent has that display name. `name` is not unique (multiple
  identities may register the same one — see repo.register_agents), so a
  collision 404s/409s instead of silently picking a winner; a caller that
  needs to pin one specific agent should use the `id` route.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from souk.config import ServingSettings
from souk.core import Souk
from souk.deps import get_serving_settings, get_souk
from souk.errors import (
    AgentNotFound,
    AmbiguousAgentName,
    InvalidRunInput,
    ThreadNotFound,
    ThreadOwnershipMismatch,
)
from souk.identity import InvalidActorChain
from souk.protocols.a2a import A2AAdapter, A2AStream

router = APIRouter()


def _adapter(souk: Souk, serving: ServingSettings) -> A2AAdapter:
    return A2AAdapter(souk, public_base_url=serving.public_http_url)


async def _resolve(adapter: A2AAdapter, name: str) -> str:
    try:
        return await adapter.resolve_agent_id(name)
    except AgentNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except AmbiguousAgentName as e:
        raise HTTPException(
            status_code=409,
            detail={
                "error": f"multiple agents are registered under the name '{e.name}'",
                "retry_with": "/agui/id/{agent_id} or /a2a/id/{agent_id}/...",
                "candidates": [
                    {
                        "name": c["name"],
                        "agent_id": c["agent_id"],
                        "public_key_prefix": c["public_key"][:12],
                        "joined_at": c["joined_at"].isoformat(),
                        "description": c["agent_card"].get("description", ""),
                    }
                    for c in e.candidates
                ],
            },
        ) from e


@router.get("/a2a/id/{agent_id}/.well-known/agent.json")
async def agent_card_by_id(
    agent_id: str,
    souk: Souk = Depends(get_souk),
    serving: ServingSettings = Depends(get_serving_settings),
) -> dict:
    try:
        return await _adapter(souk, serving).agent_card(agent_id)
    except AgentNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("/a2a/{name}/.well-known/agent.json")
async def agent_card_by_name(
    name: str,
    souk: Souk = Depends(get_souk),
    serving: ServingSettings = Depends(get_serving_settings),
) -> dict:
    adapter = _adapter(souk, serving)
    return await adapter.agent_card(await _resolve(adapter, name))


async def _rpc(adapter: A2AAdapter, agent_id: str, request: Request):
    payload = await request.json()
    try:
        result = await adapter.handle_rpc(agent_id, payload)
    except AgentNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ThreadNotFound as e:
        raise HTTPException(status_code=404, detail=f"thread '{e}' not found") from e
    except ThreadOwnershipMismatch as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except InvalidActorChain as e:
        raise HTTPException(status_code=401, detail=f"invalid actor chain: {e}") from e
    except InvalidRunInput as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    if isinstance(result, A2AStream):

        async def stream():
            async for item in result.results:
                yield {"event": "message", "data": json.dumps(item)}

        return EventSourceResponse(stream())
    return result


@router.post("/a2a/id/{agent_id}/rpc")
async def rpc_by_id(
    agent_id: str,
    request: Request,
    souk: Souk = Depends(get_souk),
    serving: ServingSettings = Depends(get_serving_settings),
):
    return await _rpc(_adapter(souk, serving), agent_id, request)


@router.post("/a2a/{name}/rpc")
async def rpc_by_name(
    name: str,
    request: Request,
    souk: Souk = Depends(get_souk),
    serving: ServingSettings = Depends(get_serving_settings),
):
    adapter = _adapter(souk, serving)
    return await _rpc(adapter, await _resolve(adapter, name), request)
