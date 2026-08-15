"""Minimal streaming A2A client: calls another agent's `message/stream`
and yields each TaskStatusUpdateEvent/TaskArtifactUpdateEvent as it
arrives. Used by agent-template's sub-agent-calling tool so a "main agent"
can watch a sub-agent's progress live instead of only seeing its final
result.

`message/stream` is the current spec's name for what was `tasks/sendSubscribe`,
and `contextId`/`taskId` now travel on the message rather than beside it.
The task id is no longer the caller's to assign either: the callee's Task
comes back with its own id, which is what a later `tasks/get` uses. This
client sent the original spelling for a long time, and nothing here noticed —
A2A is hand-written on both sides of this repo (see souk/protocols/a2a.py),
so there is no dependency whose upgrade would have said so.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator
from typing import Any

import httpx
from httpx_sse import aconnect_sse


def new_request_id() -> str:
    """A JSON-RPC request id, which is all this is. It used to mint a *task*
    id, back when the caller assigned one; the current spec has nowhere on
    the wire to put a caller-chosen task id, so the name was a leftover
    claiming something no longer true."""
    return f"req_{secrets.token_hex(12)}"


async def call_agent_streaming(
    a2a_rpc_url: str,
    message_text: str,
    *,
    request_id: str | None = None,
    context_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    actor_chain: list[str] | None = None,
    reference_task_ids: list[str] | None = None,
    timeout: float = 120.0,
) -> AsyncIterator[dict[str, Any]]:
    """`actor_chain`, if given, proves this call's identity (and, for a
    multi-hop chain, who it's ultimately acting on behalf of) to the
    callee's souk — see souk/identity.py's verify_actor_chain and
    souk_agent_sdk.identity's new_actor_chain/extend_actor_chain for how
    to build one. Entirely optional: souk doesn't require callers to
    authenticate.

    `context_id`, if given, is real A2A (`Message.contextId` — the
    caller passes back whatever `contextId` it was returned on an
    earlier call to the same callee, per the spec's own session-
    continuation convention) to continue talking to the same callee
    thread. Omit it (the default) to always start a fresh one — souk
    never reuses a thread implicitly just because `reference_task_ids`
    matches an earlier call (see souk/repo.py's ensure_thread docstring
    and souk-no-forced-protocol-deviation): lineage and continuity are
    orthogonal, a caller must opt into continuity explicitly.

    `reference_task_ids`, if given, is real A2A (`Message.referenceTaskIds`
    — "a list of other task IDs that this message references for
    additional context"), not a souk invention like the `parentThreadId`
    metadata field this replaced: pass the caller's own current task id
    (e.g. its own run_id) to let souk record the lineage (see
    souk/db.py's threads.parent_thread_id) so a later `GET /threads/
    {root}/tree` can show what a top-level call actually fanned out to.
    Exposed as an explicit parameter (rather than left for each caller to
    remember itself) so anyone using this shared client gets correct
    lineage by default. This is purely informational per the A2A spec —
    it never implies session continuity; use `context_id` for that.
    """
    request_id = request_id or new_request_id()
    metadata = dict(metadata) if metadata else {}
    if actor_chain is not None:
        metadata["actorChain"] = actor_chain

    message: dict[str, Any] = {"role": "user", "parts": [{"kind": "text", "text": message_text}]}
    if reference_task_ids:
        message["referenceTaskIds"] = reference_task_ids
    if context_id:
        message["contextId"] = context_id

    params: dict[str, Any] = {"message": message}
    if metadata:
        params["metadata"] = metadata

    body = {"jsonrpc": "2.0", "id": request_id, "method": "message/stream", "params": params}

    async with httpx.AsyncClient(timeout=timeout) as client:
        async with aconnect_sse(client, "POST", a2a_rpc_url, json=body) as event_source:
            async for sse in event_source.aiter_sse():
                payload = json.loads(sse.data)
                result = payload.get("result")
                if result is not None:
                    yield result
