# Provider examples

souk doesn't run any agent logic itself — every actual agent is a "provider"
that connects out to a souk and speaks its gRPC contract (`proto/souk.proto`).
`souk_agent_sdk` is a convenience client for that contract, not the contract
itself: any implementation of `proto/souk.proto`, in any language, is an
equally valid provider.

- **`/agent-template`** (repo root, not under here) — the minimal reference:
  the smallest possible `souk_agent_sdk.AgentHandle` implementation, no
  framework attached. Start here to understand the contract, or copy it as
  the seed for a provider written from scratch.
- **`pydantic-ai-agent/`** — a fuller example: YAML-configured agents backed
  by [pydantic-ai](https://ai.pydantic.dev), with MCP tool support and
  sub-agent delegation over A2A.

Add new provider examples as siblings of `pydantic-ai-agent/` here (e.g. a
different LLM framework, a non-Python implementation, a HITL-approval demo)
rather than growing `/agent-template` — that one stays deliberately minimal.
