"""A2A HTTP surface: routes only.

What A2A *means* — Task.id being souk's run_id, contextId being thread_id,
what tasks/send does when a session already has a live run — lives in
souk/protocols/a2a.py, in core. This file parses requests and frames results
as JSON or SSE; domain errors are translated once for the whole app (see
souk.deps.install_error_handlers), not per route.

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

from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH
from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse

from souk_server.config import ServingSettings
from souk.core import Souk
from souk_server.deps import get_serving_settings, get_souk
from souk.protocols.a2a import A2AAdapter, A2AStream

router = APIRouter()


def _adapter(souk: Souk, serving: ServingSettings) -> A2AAdapter:
    return A2AAdapter(souk, public_base_url=serving.public_http_url)


# The path comes from a2a.utils.constants rather than being typed here, for
# the same reason every other A2A string in souk does: v1.0 moved it (from
# `/.well-known/agent.json`), and souk should learn that from the package
# rather than from a client failing against it.
#
# Only this one is served. A route for the older path existed briefly, on the
# reasoning that a card is what a client finds souk *with* — but it answered
# the old URL with the *new* body, which has no top-level `url` and no
# `preferredTransport`, so a pre-v1 client found a card it could not use to
# locate the RPC endpoint. Half an accommodation is not one, and the two that
# do work — every older method name, and every older part spelling — work end
# to end (see souk/tests/test_a2a_spec_methods.py). Whether to serve the
# legacy path at all, and if so whether to answer it with a legacy-shaped
# body, is a gateway decision and belongs downstream.
@router.get("/a2a/id/{agent_id}" + AGENT_CARD_WELL_KNOWN_PATH)
async def agent_card_by_id(
    agent_id: str,
    souk: Souk = Depends(get_souk),
    serving: ServingSettings = Depends(get_serving_settings),
) -> dict:
    return await _adapter(souk, serving).agent_card(agent_id)


@router.get("/a2a/{name}" + AGENT_CARD_WELL_KNOWN_PATH)
async def agent_card_by_name(
    name: str,
    souk: Souk = Depends(get_souk),
    serving: ServingSettings = Depends(get_serving_settings),
) -> dict:
    adapter = _adapter(souk, serving)
    return await adapter.agent_card(await adapter.resolve_agent_id(name))


async def _rpc(adapter: A2AAdapter, agent_id: str, request: Request):
    result = await adapter.handle_rpc(agent_id, await request.json())
    if not isinstance(result, A2AStream):
        return result

    async def stream():
        async for data in result.encode():
            yield {"event": "message", "data": data}

    return EventSourceResponse(stream())


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
    return await _rpc(adapter, await adapter.resolve_agent_id(name), request)
