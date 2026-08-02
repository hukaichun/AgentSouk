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


class TemplateConfig(BaseModel):
    souk_http_url: str
    souk_grpc_url: str
    agents: list[AgentConfig]


def load_config(path: str | Path) -> TemplateConfig:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return TemplateConfig.model_validate(raw)
