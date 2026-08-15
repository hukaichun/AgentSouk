from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class SubAgentConfig(BaseModel):
    name: str
    a2a_url: str  # full A2A JSON-RPC URL, e.g. http://souk:8000/a2a/other-agent/rpc


class AgentConfig(BaseModel):
    name: str
    description: str = ""
    model: str
    system_prompt: str
    mcp_servers: list[str] = Field(default_factory=list)
    sub_agents: list[SubAgentConfig] = Field(default_factory=list)
    # Opt-in: gives this agent the tools in pydantic_ai_agent.souk_tools
    # (currently just list_souk_agents), which query the souk this
    # provider is itself registered on via its already-public HTTP API —
    # see that module's docstring for why this isn't a real MCP server.
    souk_tools: bool = False
    # Opt-in: Keep Your Own Key (see docs/keep-your-own-key.md). When a
    # caller's run carries forwardedProps.kyok (it's running its own KYOK
    # bridge), this agent routes that run's LLM calls through souk's
    # /kyok/v1 relay instead of `model` above — the caller pays with
    # their own key for that run only. A run without forwardedProps.kyok
    # (an ordinary caller) always falls back to `model` regardless of
    # this flag; set to False to never use KYOK even when offered, e.g.
    # if this agent's economics depend on it always paying for its own
    # tokens.
    use_kyok: bool = False


class TemplateConfig(BaseModel):
    souk_http_url: str
    # Optional storefront label for this provider's public_key — shown
    # when souk-directory groups agents by provider. Provider-level, not
    # per-agent (see souk_agent_sdk.client.SoukProvider's provider_name
    # kwarg / souk/db.py's providers table).
    provider_name: str | None = None
    agents: list[AgentConfig]


def load_config(path: str | Path) -> TemplateConfig:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return TemplateConfig.model_validate(raw)
