"""Minimal reference souk provider.

This is the floor, not a framework: it shows exactly what's required to
speak souk_agent_sdk's contract, with nothing else — no config file format,
no LLM SDK, no MCP, no sub-agents. Copy this file as the starting point for
a new provider written from scratch; if you want an LLM already wired up
(pydantic-ai, MCP tools, sub-agent delegation, HITL pause/resume), start
from one of the fuller examples in providers/ instead (see
providers/README.md).

The whole contract is: build one or more `AgentHandle(name, run_stream)`,
where `run_stream(run_input: dict) -> AsyncIterator[dict]` takes a real
AG-UI RunAgentInput (camelCase wire JSON, as built by souk.agui) and yields
AG-UI events. Hand a list of handles to SoukProvider and call
run_forever() — everything else (registration, the
persistent work socket, reconnect-on-drop) is souk_agent_sdk's job, not yours.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from souk_agent_sdk import AgentHandle, SoukProvider

logger = logging.getLogger("agent_template")


async def run_stream(run_input: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    """Echoes the last user message back. This is the whole agent — real
    ones would call an LLM here instead, but the AG-UI event shape souk
    expects is the same either way.
    """
    thread_id = run_input["threadId"]
    run_id = run_input["runId"]
    yield {"type": "RUN_STARTED", "threadId": thread_id, "runId": run_id}

    last_user_text = ""
    for message in reversed(run_input.get("messages", [])):
        if message.get("role") == "user":
            last_user_text = message.get("content", "")
            break

    # A run can also end paused instead of finished — e.g. a tool-call
    # approval: RUN_FINISHED's own outcome={"type": "interrupt", ...},
    # AG-UI's native mechanism (pydantic-ai's Tool(requires_approval=True)
    # does this for you) — then ending the stream normally either way;
    # see souk/pause.py for the full convention and providers/
    # pydantic-ai-agent for a worked example (deferred tool calls +
    # sub-agent delegation).

    message_id = f"{run_id}-reply"
    yield {"type": "TEXT_MESSAGE_START", "messageId": message_id, "role": "assistant"}
    yield {
        "type": "TEXT_MESSAGE_CONTENT",
        "messageId": message_id,
        "delta": f"echo: {last_user_text}",
    }
    yield {"type": "TEXT_MESSAGE_END", "messageId": message_id}
    yield {"type": "RUN_FINISHED", "threadId": thread_id, "runId": run_id}


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    souk_http_url = os.environ.get("SOUK_HTTP_URL", "http://localhost:8000")
    agent_name = os.environ.get("AGENT_NAME", "echo-agent")

    provider = SoukProvider(
        souk_http_url,
        agents=[AgentHandle(name=agent_name, description="Echoes back the last user message.", run_stream=run_stream)],
    )
    logger.info("starting agent-template provider: agent_name=%s souk_http_url=%s", agent_name, souk_http_url)
    await provider.run_forever()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
