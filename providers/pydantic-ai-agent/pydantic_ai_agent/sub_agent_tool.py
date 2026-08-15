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
from dataclasses import dataclass, field

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
    # This run's own task id (souk's real run_id — see main.py's
    # `run_input.get("runId")`), forwarded as a sub-agent call's
    # `referenceTaskIds` (real A2A, not a souk invention) so souk can
    # record lineage — see call_sub_agent below.
    run_id: str | None = None
    # A fresh, single-hop identity chain asserting "this call comes from
    # me" (see souk_agent_sdk.identity.new_actor_chain), built once from
    # this provider's own registered key — reused for every sub-agent
    # call in this run so the callee's souk can attribute the call to a
    # known, registered agent instead of an anonymous caller (see
    # souk/identity.py's verify_actor_chain). Optional: a sub-agent call
    # without one is still allowed, just unattributed.
    actor_chain: list[str] | None = None
    # sub_agent name -> the real contextId (A2A) souk returned on that
    # sub-agent's most recent call within this run. souk never reuses a
    # callee thread implicitly (see souk/repo.py's ensure_thread
    # docstring: lineage via referenceTaskIds and session continuity are
    # deliberately orthogonal) — a repeated call to the same sub-agent
    # within one main-agent run wants to keep talking to the same
    # sub-thread, so this call site has to opt in explicitly, the
    # standard A2A way, by passing back what it was given last time.
    sub_agent_context_ids: dict[str, str] = field(default_factory=dict)


def build_sub_agent_tools(sub_agents: list[SubAgentConfig]) -> list[Tool]:
    return [_make_tool(sub) for sub in sub_agents]


def _make_tool(sub: SubAgentConfig) -> Tool:
    async def call_sub_agent(ctx: RunContext[AgentDeps], message: str) -> str:
        # souk assigns every thread id itself (see souk.ids /
        # souk.repo.ensure_thread), the sub-agent tool never mints one.
        # `referenceTaskIds` (real A2A, see souk_agent_sdk.a2a_client)
        # tells souk which task this call references, purely so it can
        # record lineage (GET /threads/{root}/tree) — it never implies
        # reusing an earlier sub-thread with this callee (A2A's
        # referenceTaskIds is informational-only, not a session-grouping
        # primitive). Continuing the same sub-thread across repeated
        # delegations within one main conversation is instead handled
        # explicitly below via `context_id`.
        reference_task_ids = [ctx.deps.run_id] if ctx.deps.run_id else None
        # See AgentDeps.sub_agent_context_ids: pass back whatever contextId
        # this sub-agent last returned so souk continues the same
        # sub-thread instead of starting a fresh one.
        context_id = ctx.deps.sub_agent_context_ids.get(sub.name)

        final_text = ""
        failed = False
        # Set when souk's final status update for this call reports
        # 'input-required' — the callee paused (e.g. HITL approval, see
        # souk/pause.py) rather than truly finishing. Distinct from
        # "failed" or "no response": the call is still live, just not
        # resolved yet. Reported honestly here (see
        # souk.api_a2a._finalize_delegated_call — it's the one that makes
        # sure this state update reflects reality rather than the raw
        # RUN_FINISHED event a naive translation would otherwise send).
        pending = False
        async for update in call_agent_streaming(
            sub.a2a_url,
            message,
            context_id=context_id,
            reference_task_ids=reference_task_ids,
            actor_chain=ctx.deps.actor_chain,
        ):
            await ctx.deps.progress_queue.put(
                {
                    "type": "CUSTOM",
                    "name": "sub_agent_progress",
                    "value": {"sub_agent": sub.name, **update},
                }
            )
            # A2A v1.0 wraps every streamed item in a StreamResponse whose
            # single key says what it is, rather than putting a bare update on
            # the wire with a discriminator field inside it.
            status_update = update.get("statusUpdate") or {}
            artifact_update = update.get("artifactUpdate") or {}

            returned_context_id = status_update.get("contextId") or artifact_update.get("contextId")
            if returned_context_id:
                ctx.deps.sub_agent_context_ids[sub.name] = returned_context_id
            state = status_update.get("status", {}).get("state")
            if state == "TASK_STATE_FAILED":
                failed = True
            elif state == "TASK_STATE_INPUT_REQUIRED":
                pending = True
            for part in (artifact_update.get("artifact") or {}).get("parts", []):
                # `Part` is a oneof: a text part is `{"text": ...}` and
                # anything else (file, data) simply has no `text` key.
                if isinstance(part.get("text"), str):
                    final_text += part["text"]
        if failed:
            return f"({sub.name} is currently unavailable — the call failed or timed out)"
        if pending:
            return (
                f"({sub.name} needs more time to respond — e.g. it's waiting on human "
                "approval — and hasn't resolved yet; you'll be notified once it does, "
                "no need to call it again for this)"
            )
        return final_text or f"(no response from {sub.name})"

    call_sub_agent.__name__ = f"call_{sub.name}"
    call_sub_agent.__doc__ = f"Call the '{sub.name}' sub-agent via A2A and return its response."
    return Tool(
        call_sub_agent,
        name=f"call_{sub.name}",
        description=f"Call the '{sub.name}' sub-agent via A2A and return its response.",
    )
