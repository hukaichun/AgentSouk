# Prior art

This is a working comparison, not a scoreboard — it's based on public docs
and repos, not deep firsthand review of every project's source, so treat
it as a starting point and correct us (open an issue) if something's
inaccurate or stale.

souk's specific combination — outbound-only connectivity for agents
without a public IP, AG-UI *and* A2A on the same gateway, durable
run/thread state, HITL/async pause-resume, and self-sovereign identity
with no signup flow — doesn't appear to exist as a single project
elsewhere. But no individual piece of it is new; several adjacent
projects, some backed by far more resources than this one, are solving
overlapping slices:

| Project | Solves | Doesn't solve (vs. souk) |
|---|---|---|
| [**a2a-relay**](https://github.com/zeroasterisk/a2a-relay) | Outbound WebSocket relay for agents without a public URL — the closest architectural match to souk's core mechanism | A2A only (no AG-UI); shared-secret JWT auth (no per-agent identity — one relay-wide secret can mint a token for *any* agent_id, which is exactly the impersonation gap souk's provider identity closes); stateless forwarder, no persisted history; no HITL |
| [**agentgateway**](https://agentgateway.dev/) | Security/observability/governance proxy in front of A2A/MCP backends | Assumes backends are already reachable — solves a different problem (governing traffic to infra you already control), not NAT traversal or self-service onboarding |
| [**AgentConnect / ANP**](https://github.com/agent-network-protocol/AgentConnect) | W3C DID-based decentralized agent identity, protocol negotiation | Not a relay — assumes agents have stable reachable endpoints; no marketplace/billing; pre-1.0 (~337 stars at time of writing) |
| [**NANDA**](https://nanda.media.mit.edu/) (MIT) | Federated registry: discovery/trust/economic-incentive layer explicitly designed to sit *under* A2A/MCP, not replace them | Doesn't address connectivity at all; academic-stage, implementation details of the federation mechanics aren't public |
| [**Google Cloud AI Agent Marketplace**](https://cloud.google.com/blog/topics/partners/google-cloud-ai-agent-marketplace) | A real, live A2A-based marketplace with global distribution | Requires the agent already be deployed somewhere reachable (typically GCP); ties discovery to one vendor's platform |
| Agent-economy platforms (toku.agency, etc.) | Real transactions between agents happening today | Pure matchmaking/billing layer, no connectivity story |
| [**Nevermined**](https://nevermined.ai/) / [**x402**](https://www.x402.org/) | Payment/billing rails for agent-to-agent transactions (x402 has real production volume) | Not connectivity or identity — this is exactly the kind of thing souk should integrate with later rather than reinvent |
| Anthropic MCP Tunnels / OpenAI Secure MCP Tunnel | Outbound-only relay so a hosted LLM can reach tools behind a firewall — **architecturally the same core pattern as souk** | MCP tool-calling only, not agent-to-agent; closed (works only with that vendor's own model); not an open protocol; no marketplace angle |
| [**QM**](https://github.com/yc-software/qm) | Multi-user "agent harness" one org deploys for itself — per-user/room scoped sandboxes, "agent acts as the person, with their permissions," tiered approval modes | Single-org deployment, not a network multiple parties join independently; bundles execution/sandboxing with the collaboration layer (souk deliberately keeps those separate) — but its identity model (act on behalf of a user, carrying their permissions) is exactly what souk's actor-chain mechanism generalizes |

**Read as a category, not a single competitor**: the "outbound relay for
unreachable agents" pattern is validated by Anthropic and OpenAI shipping
it (for MCP specifically); the "open marketplace" pattern is validated by
Google Cloud and the agent-economy platforms; the "federated
discovery/identity" pattern is being explored by NANDA and ANP. souk's bet
is that the intersection of all of these — reachable *and* open *and*
persistent *and* HITL-aware — is where the actual gap is, not any one
axis alone.
