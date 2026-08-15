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

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.ui.ag_ui import AGUIAdapter
from souk_agent_sdk import AgentHandle, KyokSigningAuth, SoukProvider
from souk_agent_sdk.identity import load_or_create_identity, new_actor_chain, public_key_hex

from pydantic_ai_agent.config import AgentConfig, load_config
from pydantic_ai_agent.souk_tools import build_souk_tools
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


def resolve_kyok_model(
    run_input: dict[str, Any], souk_http_url: str, signing_key: Ed25519PrivateKey
) -> OpenAIChatModel | None:
    """Keep Your Own Key (see docs/keep-your-own-key.md): if this run's
    caller is offering to pay with their own key, souk.api_agui put a
    run-scoped token at forwardedProps.kyok.token — build a model pointed
    at souk's /kyok/v1 relay instead of this agent's own configured
    `model`. The base URL is `souk_http_url` (the same address this
    process already registers/polls/calls A2A against), not anything
    souk hands back — souk's own public_http_url is for external callers
    and is frequently unreachable from inside a provider's own
    container/network (see docker-compose.yml). The `model` name here is
    never seen by any real LLM (souk's relay doesn't interpret it, and
    the caller's bridge picks its own real model independently) — it
    only needs to satisfy the OpenAI request schema's required field, so
    a fixed placeholder is fine. Returns None if this run carries no such
    offer, meaning the caller wasn't set up for KYOK for whatever reason.

    The token alone only proves souk minted it — not who's presenting it
    on any given call. `KyokSigningAuth` (souk_agent_sdk) signs every
    request with this same process's registration identity so souk can
    verify, per call, that it's genuinely this agent making it — see
    that module's docstring and souk.api_llm_bridge.chat_completions for
    the other half of this check.
    """
    kyok = (run_input.get("forwardedProps") or {}).get("kyok") if isinstance(run_input.get("forwardedProps"), dict) else None
    if not kyok:
        return None
    http_client = httpx.AsyncClient(auth=KyokSigningAuth(signing_key))
    provider = OpenAIProvider(
        base_url=f"{souk_http_url.rstrip('/')}/kyok/v1", api_key=kyok["token"], http_client=http_client
    )
    return OpenAIChatModel("kyok", provider=provider)


def build_pydantic_agent(cfg: AgentConfig, souk_http_url: str) -> Agent:
    toolsets = [MCPToolset(url) for url in cfg.mcp_servers]
    tools = build_sub_agent_tools(cfg.sub_agents)
    if cfg.souk_tools:
        tools += build_souk_tools(souk_http_url)
    return Agent(
        resolve_model(cfg.model),
        system_prompt=cfg.system_prompt,
        toolsets=toolsets,
        tools=tools,
        deps_type=AgentDeps,
    )


def make_run_stream(agent: Agent, signing_key, souk_http_url: str, use_kyok: bool = False):
    async def run_stream(run_input: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        # `combined` is where the AG-UI adapter's own events AND any
        # sub-agent CUSTOM progress events (pushed by tools via AgentDeps)
        # both land, so they interleave in real time rather than the
        # progress only surfacing after the fact.
        # run_input is a real AG-UI RunAgentInput JSON dict from souk (see
        # souk.agui.build_run_agent_input) — camelCase wire keys.
        combined: asyncio.Queue = asyncio.Queue()
        # Built fresh per run, not once at startup — chains are
        # short-lived (souk_agent_sdk.identity.ACTOR_CHAIN_TTL_SECONDS),
        # a process that lives longer than that would otherwise hand
        # sub-agent calls an already-expired chain.
        actor_chain = new_actor_chain(
            signing_key, subject={"type": "agent", "publicKey": public_key_hex(signing_key)}
        )
        deps = AgentDeps(
            progress_queue=combined,
            thread_id=run_input.get("threadId"),
            run_id=run_input.get("runId"),
            actor_chain=actor_chain,
        )

        kyok_model = resolve_kyok_model(run_input, souk_http_url, signing_key) if use_kyok else None

        async def drain_adapter() -> None:
            try:
                run_input_obj = AGUIAdapter.build_run_input(json.dumps(run_input).encode())
                adapter = AGUIAdapter(agent=agent, run_input=run_input_obj)
                async for event in adapter.run_stream(deps=deps, model=kyok_model):
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

    # Loaded once up front (not inside SoukProvider) so the exact same
    # identity is available here for signing sub-agent actor chains (see
    # sub_agent_tool.AgentDeps.actor_chain) — SoukProvider loads the
    # same on-disk key itself for registration, so both end up with
    # identical keys without this being passed between them.
    identity_key_path = os.environ.get("SOUK_IDENTITY_KEY_PATH", "souk_identity.key")
    signing_key = load_or_create_identity(identity_key_path)

    handles = []
    for agent_cfg in cfg.agents:
        agent = build_pydantic_agent(agent_cfg, cfg.souk_http_url)
        handles.append(
            AgentHandle(
                name=agent_cfg.name,
                description=agent_cfg.description,
                run_stream=make_run_stream(
                    agent, signing_key, cfg.souk_http_url, use_kyok=agent_cfg.use_kyok
                ),
            )
        )
        logger.info("built pydantic-ai agent '%s' (model=%s)", agent_cfg.name, agent_cfg.model)

    provider = SoukProvider(
        cfg.souk_http_url,
        handles,
        identity_key_path=identity_key_path,
        provider_name=cfg.provider_name,
    )
    await provider.run_forever()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
