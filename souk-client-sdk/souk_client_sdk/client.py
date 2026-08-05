"""Caller-side SDK: a thin AG-UI client for talking to an agent through a
souk, so callers (human-facing apps, or another agent acting as a plain
top-level caller) don't have to hand-roll HTTP+SSE or thread bookkeeping.

Not agent-facing — unrelated to souk-agent-sdk's poll/gRPC machinery.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx
from httpx_sse import aconnect_sse


class SoukClient:
    def __init__(self, souk_http_url: str, timeout: float = 300.0) -> None:
        self.souk_http_url = souk_http_url.rstrip("/")
        self.timeout = timeout
        self.last_thread_id: str | None = None
        self.last_run_id: str | None = None

    async def run(
        self,
        agent_name: str,
        message: str = "",
        *,
        thread_id: str | None = None,
        role: str = "user",
        metadata: dict[str, Any] | None = None,
        resume: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """POSTs a RunAgentInput to /agui/{agent_name} and yields each AG-UI
        event as it streams back. Pass `thread_id` from a previous call's
        `last_thread_id` to continue that conversation; omit it to start a
        new one (souk assigns and returns the id).

        `metadata` is stored on the run/thread as-is and, notably, is
        where a Keep Your Own Key caller passes
        `{"kyok": {"sessionId": ...}}` — see KyokBridge and
        docs/keep-your-own-key.md in the souk repo.

        `resume` is AG-UI's own interrupt/resume mechanism
        (`ag_ui.core.ResumeEntry`: `{"interruptId": ..., "status":
        "resolved"|"cancelled", "payload": ...}`) — pass it, on the same
        `thread_id` a previous call's stream ended paused on (its last
        `RUN_FINISHED` carried `outcome.type == "interrupt"`), to resolve
        one or more of those interrupts. `message` isn't required
        alongside it — resolving an interrupt isn't necessarily saying
        anything new in the conversation, so an empty `message` sends no
        message at all rather than an empty one.
        """
        body: dict[str, Any] = {"thread_id": thread_id, "messages": []}
        if message:
            body["messages"] = [{"id": str(uuid4()), "role": role, "content": message}]
        if metadata is not None:
            body["metadata"] = metadata
        if resume is not None:
            body["resume"] = resume
        url = f"{self.souk_http_url}/agui/{agent_name}"

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with aconnect_sse(client, "POST", url, json=body) as event_source:
                self.last_thread_id = event_source.response.headers.get("X-Souk-Thread-Id")
                self.last_run_id = event_source.response.headers.get("X-Souk-Run-Id")
                async for sse in event_source.aiter_sse():
                    yield json.loads(sse.data)
