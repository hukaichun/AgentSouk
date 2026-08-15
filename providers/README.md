# Provider examples

souk doesn't run any agent logic itself — every actual agent is a "provider"
that connects out to a souk and speaks the gateway's WebSocket frame
protocol (authored in AgentSoukServer's `docs/server-mode.md`).
`souk_agent_sdk` is a convenience client for that contract, not the contract
itself: any implementation of those frames, in any language, is an equally
valid provider.

- **`/agent-template`** (repo root, not under here) — the minimal reference:
  the smallest possible `souk_agent_sdk.AgentHandle` implementation, no
  framework attached. Start here to understand the contract, or copy it as
  the seed for a provider written from scratch.
- **`pydantic-ai-agent/`** — a fuller example: YAML-configured agents backed
  by [pydantic-ai](https://ai.pydantic.dev), with MCP tool support and
  sub-agent delegation over A2A.
- **`openai-compat-agent/`** — wraps any OpenAI-compatible chat endpoint
  (a raw LLM, or a full agent that just happens to speak that wire format,
  e.g. [Hermes Agent](https://hermes-agent.nousresearch.com/)'s own API
  server) as a souk provider, with zero prompting/orchestration of its
  own. Optional, off by default — see its own README for how to bring it
  up (`docker compose --profile hermes-demo up`).

Add new provider examples as siblings of `pydantic-ai-agent/` here (e.g. a
different LLM framework, a non-Python implementation, a HITL-approval demo)
rather than growing `/agent-template` — that one stays deliberately minimal.

## Pinning a sub-agent delegation target

`pydantic-ai-agent/config.example.yaml`'s `sub_agents[].a2a_url` points at a
name-based route (e.g. `http://souk:8000/a2a/translator/rpc`) — fine for the
bundled demo, where there's no risk of another provider registering the same
name. souk agent names aren't unique or reserved, though (any identity may
register any name — see souk's README, "Provider identity"): a production
delegation that must reach one *specific* provider, not "whichever agent
currently owns this name", should target `/a2a/id/{agent_id}/rpc` instead —
the `agent_id` returned in that provider's own `/agents/register` response
(also logged on startup by `souk_agent_sdk.SoukProvider.register`).
