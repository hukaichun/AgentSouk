"""Wraps any OpenAI-compatible chat endpoint as a souk provider — a raw
LLM, or a full agent (tools, memory, its own multi-step reasoning) that
just happens to speak that wire format for convenience, e.g. Hermes
Agent's API server (https://hermes-agent.nousresearch.com/docs/
user-guide/features/api-server): "exposes hermes-agent as an
OpenAI-compatible HTTP endpoint that any frontend speaking the OpenAI
format ... can connect to and use as a backend." Souk doesn't care which
kind it's pointed at — either way this file's job is the same: relay
this run's AG-UI messages to `{api_base}/chat/completions` and translate
the streamed response back into AG-UI text events.

Deliberately a dumb relay: no tool orchestration, no memory of its own,
no thread/session continuity beyond whatever `messages` the caller (souk)
already reconstructed for this run. Whatever intelligence the wrapped
endpoint has is entirely its own — see config.py's optional
`system_prompt` for the one exception, which is no more than any plain
OpenAI API caller could already do.

Pause/resume (HITL) is out of scope here on purpose, not an oversight:
this wraps a single stateless request/response call, and a genuinely
async agent behind one of these endpoints (one that might need to pause
mid-run) doesn't fit this shape at all — see docs/agent-provider-guide.md
for why a facade can't paper over that gap safely. If your wrapped
endpoint ever needs HITL, don't front it with this template.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
from httpx_sse import aconnect_sse
from souk_agent_sdk import AgentHandle, SoukProvider

from openai_compat_agent.config import AgentConfig, load_config

logger = logging.getLogger("openai_compat_agent")


def _build_messages(agent: AgentConfig, run_input: dict[str, Any]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    if agent.system_prompt:
        messages.append({"role": "system", "content": agent.system_prompt})
    for message in run_input.get("messages", []):
        role = message.get("role")
        content = message.get("content") or ""
        if role in ("user", "assistant", "system") and content:
            messages.append({"role": role, "content": content})
    return messages


def make_run_stream(agent: AgentConfig):
    async def run_stream(run_input: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        thread_id = run_input["threadId"]
        run_id = run_input["runId"]
        yield {"type": "RUN_STARTED", "threadId": thread_id, "runId": run_id}

        message_id = f"{run_id}-reply"
        body = {"model": agent.model, "messages": _build_messages(agent, run_input), "stream": True}
        headers = {"Authorization": f"Bearer {agent.api_key}"} if agent.api_key else {}
        if agent.session_id_header:
            headers[agent.session_id_header] = thread_id
        if agent.memory_scope_header:
            headers[agent.memory_scope_header] = thread_id
        url = f"{agent.api_base.rstrip('/')}/chat/completions"

        started = False
        try:
            async with httpx.AsyncClient(timeout=agent.timeout_seconds) as client:
                async with aconnect_sse(client, "POST", url, json=body, headers=headers) as event_source:
                    async for sse in event_source.aiter_sse():
                        if sse.data == "[DONE]":
                            break
                        chunk = json.loads(sse.data)
                        for choice in chunk.get("choices", []):
                            delta = choice.get("delta", {}).get("content")
                            if not delta:
                                continue
                            if not started:
                                yield {
                                    "type": "TEXT_MESSAGE_START",
                                    "messageId": message_id,
                                    "role": "assistant",
                                }
                                started = True
                            yield {"type": "TEXT_MESSAGE_CONTENT", "messageId": message_id, "delta": delta}
        except Exception as e:
            logger.exception("openai-compat-agent %s: upstream call to %s failed", agent.name, url)
            yield {"type": "RUN_ERROR", "message": f"upstream call failed: {e}"}
            return

        if started:
            yield {"type": "TEXT_MESSAGE_END", "messageId": message_id}
        yield {"type": "RUN_FINISHED", "threadId": thread_id, "runId": run_id}

    return run_stream


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    config_path = os.environ.get("OPENAI_COMPAT_CONFIG", "config.yaml")
    cfg = load_config(config_path)

    handles = [
        AgentHandle(name=agent.name, description=agent.description, run_stream=make_run_stream(agent))
        for agent in cfg.agents
    ]
    for agent in cfg.agents:
        logger.info("wrapping OpenAI-compatible endpoint as agent '%s': api_base=%s", agent.name, agent.api_base)

    provider = SoukProvider(
        cfg.souk_http_url, handles, provider_name=cfg.provider_name
    )
    await provider.run_forever()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
