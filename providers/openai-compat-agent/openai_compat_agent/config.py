from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel


class AgentConfig(BaseModel):
    name: str
    description: str = ""
    # Any OpenAI-compatible base URL (no trailing /chat/completions) —
    # could be a raw LLM host, or a full agent's own API server that
    # happens to speak this wire format for convenience (e.g. Hermes
    # Agent: https://hermes-agent.nousresearch.com/docs/user-guide/
    # features/api-server). Souk doesn't know or care which.
    api_base: str
    api_key: str = ""
    model: str
    # Forwarded as a leading system-role message on every call — the one
    # piece of prompting this wrapper does on your behalf, since it's
    # exactly what any OpenAI API caller could already do. Leave empty to
    # relay messages completely unmodified (the wrapped endpoint's own
    # personality/system prompt, if any, is entirely its own business).
    system_prompt: str = ""
    timeout_seconds: float = 120.0
    # Some wrapped endpoints are stateless by default but accept an
    # opt-in header to scope continuity/memory to a caller-supplied id
    # (e.g. Hermes Agent's `X-Hermes-Session-Id`/`X-Hermes-Session-Key` —
    # see https://hermes-agent.nousresearch.com/docs/user-guide/features/
    # api-server: without one, Hermes derives its own session id from
    # the conversation's first message, which two different souk threads
    # can collide on, and its long-term memory isn't scoped per caller at
    # all). If set, souk's own `threadId` for this run is forwarded under
    # this header name, giving each souk thread a distinct, stable
    # identity on the wrapped side instead of relying on its own
    # heuristics. Leave unset for endpoints that don't support or need this
    # (a raw stateless LLM has no use for it).
    session_id_header: str = ""
    memory_scope_header: str = ""


class OpenAICompatConfig(BaseModel):
    souk_http_url: str
    provider_name: str | None = None
    agents: list[AgentConfig]


def load_config(path: str | Path) -> OpenAICompatConfig:
    """`${VAR}`/`$VAR` in the raw YAML text are expanded against this
    process's environment before parsing (stdlib `os.path.expandvars`) —
    lets a config commit real structure (which agents, what personas)
    while keeping secrets (api_key) and host-specific values (api_base
    behind a docker-compose network) out of the file itself.
    """
    with open(path) as f:
        raw = os.path.expandvars(f.read())
    return OpenAICompatConfig.model_validate(yaml.safe_load(raw))
