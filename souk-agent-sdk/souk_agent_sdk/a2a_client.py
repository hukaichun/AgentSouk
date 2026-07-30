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
    session_id: str | None = None,
    timeout: float = 120.0,
) -> AsyncIterator[dict[str, Any]]:
    task_id = task_id or new_task_id()
    params: dict[str, Any] = {
        "id": task_id,
        "message": {"role": "user", "parts": [{"type": "text", "text": message_text}]},
    }
    if session_id:
        params["sessionId"] = session_id

    body = {"jsonrpc": "2.0", "id": task_id, "method": "tasks/sendSubscribe", "params": params}

    async with httpx.AsyncClient(timeout=timeout) as client:
        async with aconnect_sse(client, "POST", a2a_rpc_url, json=body) as event_source:
            async for sse in event_source.aiter_sse():
                payload = json.loads(sse.data)
                result = payload.get("result")
                if result is not None:
                    yield result
