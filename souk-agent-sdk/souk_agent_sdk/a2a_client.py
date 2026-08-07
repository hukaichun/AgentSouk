"""Minimal streaming A2A client: calls another agent's `tasks/sendSubscribe`
and yields each TaskStatusUpdateEvent/TaskArtifactUpdateEvent as it
arrives. Used by agent-template's sub-agent-calling tool so a "main agent"
can watch a sub-agent's progress live instead of only seeing its final
result.

Per the A2A protocol, the caller (not the callee) assigns the task id —
that's what lets the caller later call tasks/get with the same id.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator
from typing import Any

import httpx
from httpx_sse import aconnect_sse


def new_task_id() -> str:
    return f"task_{secrets.token_hex(12)}"


async def call_agent_streaming(
    a2a_rpc_url: str,
    message_text: str,
    *,
    task_id: str | None = None,
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
    task_id = task_id or new_task_id()
    metadata = dict(metadata) if metadata else {}
    if actor_chain is not None:
        metadata["actorChain"] = actor_chain

    message: dict[str, Any] = {"role": "user", "parts": [{"type": "text", "text": message_text}]}
    if reference_task_ids:
        message["referenceTaskIds"] = reference_task_ids

    params: dict[str, Any] = {"id": task_id, "message": message}
    if context_id:
        params["contextId"] = context_id
    if metadata:
        params["metadata"] = metadata

    body = {"jsonrpc": "2.0", "id": task_id, "method": "tasks/sendSubscribe", "params": params}

    async with httpx.AsyncClient(timeout=timeout) as client:
        async with aconnect_sse(client, "POST", a2a_rpc_url, json=body) as event_source:
            async for sse in event_source.aiter_sse():
                payload = json.loads(sse.data)
                result = payload.get("result")
                if result is not None:
                    yield result
