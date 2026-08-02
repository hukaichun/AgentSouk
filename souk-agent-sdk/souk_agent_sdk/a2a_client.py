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
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx_sse import aconnect_sse

from souk_agent_sdk.identity import a2a_call_signing_payload, public_key_hex, sign


def new_task_id() -> str:
    return f"task_{secrets.token_hex(12)}"


async def call_agent_streaming(
    a2a_rpc_url: str,
    message_text: str,
    *,
    task_id: str | None = None,
    session_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    signing_key: Ed25519PrivateKey | None = None,
    timeout: float = 120.0,
) -> AsyncIterator[dict[str, Any]]:
    """`signing_key`, if given, proves this call's identity to the callee's
    souk (see souk/identity.py's a2a_call_signing_payload) — pass the same
    keypair this provider registered with (souk_agent_sdk.identity) to let
    a sub-agent call be attributed back to a known, registered agent
    rather than arriving anonymous. Entirely optional: souk doesn't
    require callers to authenticate.
    """
    task_id = task_id or new_task_id()
    metadata = dict(metadata) if metadata else {}
    if signing_key is not None:
        timestamp = int(time.time())
        payload = a2a_call_signing_payload(task_id, session_id, timestamp)
        metadata["callerPublicKey"] = public_key_hex(signing_key)
        metadata["callerSignature"] = sign(signing_key, payload)
        metadata["callerTimestamp"] = timestamp

    params: dict[str, Any] = {
        "id": task_id,
        "message": {"role": "user", "parts": [{"type": "text", "text": message_text}]},
    }
    if session_id:
        params["sessionId"] = session_id
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
