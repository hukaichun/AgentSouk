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

    async def create_thread(self, agent_name: str, *, metadata: dict[str, Any] | None = None) -> str:
        """The only way to obtain a thread_id — souk has no implicit-
        creation path anywhere; every run must address one this already
        returned. `run()` below calls this for you if you don't pass a
        `thread_id` yourself, so you only need to call this directly if
        you want the id *before* sending a first message (e.g. to show it
        in a UI right away).
        """
        url = f"{self.souk_http_url}/threads/{agent_name}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json={"metadata": metadata} if metadata else {})
            resp.raise_for_status()
            return resp.json()["thread_id"]

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
        new one — this calls `create_thread` for you in that case (souk
        itself never creates one implicitly, but there's no reason you
        should have to make two calls just to start a fresh conversation).

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
        if thread_id is None:
            thread_id = await self.create_thread(agent_name)

        # The real ag_ui.core.RunAgentInput wire shape — threadId is the
        # only id souk actually uses; runId is required by the schema but
        # never read, so a placeholder satisfies it without meaning
        # anything.
        body: dict[str, Any] = {
            "threadId": thread_id,
            "runId": str(uuid4()),
            "state": None,
            "messages": [],
            "tools": [],
            "context": [],
            "forwardedProps": None,
        }
        if message:
            body["messages"] = [{"id": str(uuid4()), "role": role, "content": message}]
        if metadata is not None:
            body["metadata"] = metadata
        if resume is not None:
            body["resume"] = resume
        url = f"{self.souk_http_url}/agui/{agent_name}"

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with aconnect_sse(client, "POST", url, json=body) as event_source:
                # No custom header to read here — a real run's first
                # event is always RUN_STARTED, carrying the resolved,
                # real threadId/runId (souk substitutes its own if
                # `thread_id` above was unrecognized) — that's the
                # standard AG-UI place to learn them, not a souk-invented
                # side channel.
                self.last_thread_id = thread_id
                async for sse in event_source.aiter_sse():
                    event = json.loads(sse.data)
                    if event.get("type") == "RUN_STARTED":
                        self.last_thread_id = event.get("threadId", thread_id)
                        self.last_run_id = event.get("runId")
                    yield event
