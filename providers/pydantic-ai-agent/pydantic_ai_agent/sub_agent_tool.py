"""Builds one pydantic-ai tool per declared sub-agent. Calling the tool
drives the sub-agent over A2A (tasks/sendSubscribe) and re-emits every
progress update it streams back as an AG-UI CUSTOM event on the *same*
queue the enclosing run's own AG-UI events are being written to (see
pydantic_ai_agent/main.py) — so sub-agent progress is visible end-to-end to
whoever is watching the main agent's run, not just consumed internally by
the tool-call loop.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic_ai import RunContext, Tool
from souk_agent_sdk.a2a_client import call_agent_streaming

from pydantic_ai_agent.config import SubAgentConfig


@dataclass
class AgentDeps:
    # Shared with the AG-UI event stream being produced for the current run
    # (see agent_template.main.make_run_stream) — pushing here interleaves
    # directly into the caller-visible output.
    progress_queue: asyncio.Queue
    thread_id: str | None = None
    # This provider's own registered identity (see
    # souk_agent_sdk.identity) — reused to sign outgoing sub-agent calls
    # so the callee's souk can attribute them to a known, registered
    # agent instead of an anonymous caller (see souk/identity.py's
    # a2a_call_signing_payload). Optional: a sub-agent call without one
    # is still allowed, just unattributed.
    signing_key: Ed25519PrivateKey | None = None


def build_sub_agent_tools(sub_agents: list[SubAgentConfig]) -> list[Tool]:
    return [_make_tool(sub) for sub in sub_agents]


def _make_tool(sub: SubAgentConfig) -> Tool:
    async def call_sub_agent(ctx: RunContext[AgentDeps], message: str) -> str:
        # No session_id here — souk assigns every thread id itself (see
        # souk.ids / souk.repo.ensure_thread), the sub-agent tool never
        # mints one. We only tell souk which thread this call is spawned
        # from (metadata.parentThreadId); souk reuses the existing child
        # thread for this (parent, sub-agent) pair if one already exists
        # (so repeated delegations within one main conversation keep
        # talking to the same sub-thread), or assigns a fresh one.
        main_thread_id = ctx.deps.thread_id

        final_text = ""
        async for update in call_agent_streaming(
            sub.a2a_url,
            message,
            metadata={"parentThreadId": main_thread_id} if main_thread_id else None,
            signing_key=ctx.deps.signing_key,
        ):
            await ctx.deps.progress_queue.put(
                {
                    "type": "CUSTOM",
                    "name": "sub_agent_progress",
                    "value": {"sub_agent": sub.name, **update},
                }
            )
            artifact = update.get("artifact")
            if artifact:
                for part in artifact.get("parts", []):
                    if part.get("type") == "text":
                        final_text += part["text"]
        return final_text or f"(no response from {sub.name})"

    call_sub_agent.__name__ = f"call_{sub.name}"
    call_sub_agent.__doc__ = f"Call the '{sub.name}' sub-agent via A2A and return its response."
    return Tool(
        call_sub_agent,
        name=f"call_{sub.name}",
        description=f"Call the '{sub.name}' sub-agent via A2A and return its response.",
    )
