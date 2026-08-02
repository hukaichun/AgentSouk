"""Generic pydantic-ai agent runner: config (system prompt + MCP servers +
sub-agents) in, souk-connected AG-UI agent(s) out. One container = one
souk-agent-sdk client = one batch of agents (one per entry in config.yaml).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.ui.ag_ui import AGUIAdapter
from souk_agent_sdk import AgentHandle, SoukAgentClient

from pydantic_ai_agent.config import AgentConfig, load_config
from pydantic_ai_agent.sub_agent_tool import AgentDeps, build_sub_agent_tools

logger = logging.getLogger("pydantic_ai_agent")

# Sentinel marking the end of a run's merged event stream.
_DONE = object()


def resolve_model(model: str) -> str | OpenAIChatModel:
    """Model strings are normally passed straight through to pydantic-ai
    (e.g. "anthropic:claude-...", "openai:gpt-..." — provider is fully
    open per-agent, not fixed). The one exception is `custom-openai:`, for
    OpenAI-compatible endpoints that aren't api.openai.com (Azure AI,
    self-hosted gateways, ...): it builds an OpenAIChatModel pointed at
    LLM_BASE_URL/LLM_API_KEY from the environment. `custom-openai` with no
    model name after the colon falls back to LLM_MODEL_NAME.
    """
    if model == "custom-openai" or model.startswith("custom-openai:"):
        model_name = model.removeprefix("custom-openai:").removeprefix("custom-openai") or os.environ[
            "LLM_MODEL_NAME"
        ]
        provider = OpenAIProvider(
            base_url=os.environ["LLM_BASE_URL"], api_key=os.environ["LLM_API_KEY"]
        )
        return OpenAIChatModel(model_name, provider=provider)
    return model


def build_pydantic_agent(cfg: AgentConfig) -> Agent:
    toolsets = [MCPToolset(url) for url in cfg.mcp_servers]
    return Agent(
        resolve_model(cfg.model),
        system_prompt=cfg.system_prompt,
        toolsets=toolsets,
        tools=build_sub_agent_tools(cfg.sub_agents),
        deps_type=AgentDeps,
    )


def make_run_stream(agent: Agent):
    async def run_stream(run_input: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        # `combined` is where the AG-UI adapter's own events AND any
        # sub-agent CUSTOM progress events (pushed by tools via AgentDeps)
        # both land, so they interleave in real time rather than the
        # progress only surfacing after the fact.
        # run_input is a real AG-UI RunAgentInput JSON dict from souk (see
        # souk.agui.build_run_agent_input) — camelCase wire keys.
        combined: asyncio.Queue = asyncio.Queue()
        deps = AgentDeps(progress_queue=combined, thread_id=run_input.get("threadId"))

        async def drain_adapter() -> None:
            try:
                run_input_obj = AGUIAdapter.build_run_input(json.dumps(run_input).encode())
                adapter = AGUIAdapter(agent=agent, run_input=run_input_obj)
                async for event in adapter.run_stream(deps=deps):
                    # by_alias=True: AG-UI's wire format is camelCase
                    # (messageId, rawEvent, ...), not the Python field names.
                    await combined.put(event.model_dump(mode="json", by_alias=True))
            except Exception:
                logger.exception("agent run failed for run_id=%s", run_input.get("runId"))
            finally:
                await combined.put(_DONE)

        task = asyncio.create_task(drain_adapter())
        try:
            while True:
                item = await combined.get()
                if item is _DONE:
                    break
                yield item
        finally:
            task.cancel()

    return run_stream


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    config_path = os.environ.get("AGENT_TEMPLATE_CONFIG", "config.yaml")
    cfg = load_config(config_path)

    handles = []
    for agent_cfg in cfg.agents:
        agent = build_pydantic_agent(agent_cfg)
        handles.append(
            AgentHandle(
                name=agent_cfg.name,
                description=agent_cfg.description,
                run_stream=make_run_stream(agent),
            )
        )
        logger.info("built pydantic-ai agent '%s' (model=%s)", agent_cfg.name, agent_cfg.model)

    client = SoukAgentClient(cfg.souk_http_url, cfg.souk_grpc_url, handles)
    await client.run_forever()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
