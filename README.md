# Agent Souk

A **souk** is a public-IP server that agents without a public IP (behind
NAT, running locally, on your own laptop, ...) connect out to and become
reachable through. Once joined, humans or other agents can interact with
an agent via the souk over two protocols:

- **AG-UI** — event-streaming, human/frontend-facing.
- **A2A** — JSON-RPC task protocol, agent-to-agent.

Agents can't be pushed to, so the agent-side SDK polls the souk for
pending work and, once it finds any, opens a gRPC stream to relay AG-UI
events for that run.

## Vision

The point of souk isn't to be a platform you deploy your agents *into* —
it's an open relay layer any agent can plug into without giving up
anything. **Anyone can run a souk. Any agent, in any language, that
speaks souk's small gRPC contract can join one** — there's no account
system gating who's allowed to participate, no vendor lock-in, and no
requirement that your agent live on anyone's infrastructure but your own.
The same mechanism that lets a company put its whole team's agents behind
one internal souk (each department its own provider, discoverable by the
rest of the org) also lets a single developer expose the agent running on
their own laptop directly — to a manager, a teammate, or another agent
somewhere else — without deploying anything anywhere.

That's the bet: most of what makes an agent useful to *other* people
depends on it being reachable and composable, not on which platform hosts
it. souk tries to be the thin, boring layer that makes reachability a
non-issue, so the interesting work stays in the agents themselves.

**Where this stands today**: the core relay (outbound connectivity, dual
AG-UI/A2A protocol support, durable run/thread history, HITL and
async-task pause/resume, self-sovereign provider and caller identity) is
built and tested — see [Provider identity](#provider-identity) below. What
the vision still needs — letting a souk you've never heard of be
discoverable and trustworthy, real payment rails, a human-browsable
directory — is not built yet; see [Roadmap](#roadmap). For how this
compares to nearby projects (tunneling tools, agent marketplaces,
decentralized identity protocols) and where the real gaps are, see
[`docs/prior-art.md`](docs/prior-art.md).

## Components

| Path | What it is |
|---|---|
| `proto/souk.proto` | gRPC contract between souk and souk-agent-sdk |
| `souk/` | The gateway server: AG-UI + A2A HTTP surface, gRPC relay, Postgres/ParadeDB persistence |
| `souk-agent-sdk/` | Agent-side SDK: registers a batch of agents, polls for work, relays runs over gRPC, includes a streaming A2A client for calling sub-agents |
| `souk-client-sdk/` | Caller-side SDK: thin AG-UI client for talking to an agent through a souk |
| `agent-template/` | Minimal reference provider: the smallest possible `souk_agent_sdk.AgentHandle` implementation, no framework attached. Copy this to start a new provider from scratch |
| `providers/` | Fuller example providers (see `providers/README.md`) — currently `pydantic-ai-agent/`, a YAML-configured pydantic-ai agent runner with MCP tool support and A2A sub-agent delegation |

souk itself runs no agent logic — every agent is a "provider" that speaks `proto/souk.proto` and connects out to a souk. `souk-agent-sdk` is a convenience client for that contract, not the contract itself: any implementation (any language) is an equally valid provider. `agent-template/` and `providers/*` are examples, not the only way to build one.

**Each of these is an independent project sharing a repo, not one coupled unit** — there's deliberately no shared workspace lockfile tying them together, so any one of them can be built, shipped, or forked on its own. `souk` never imports anything from `souk-agent-sdk` or vice versa; a provider only ever depends on `souk-agent-sdk` (a plain path/git dependency, not a workspace member). Each subdirectory has its own `pyproject.toml` and is synced independently: `cd souk && uv sync`, `cd agent-template && uv sync`, etc. — not one `uv sync` at the repo root covering everything.

## Running locally

```bash
cp .env.example .env   # fill in LLM_BASE_URL/LLM_MODEL_NAME/LLM_API_KEY (or ANTHROPIC_API_KEY — see .env.example)
docker compose up --build
```

`config.example.yaml`'s agents use `model: custom-openai`, which resolves against `LLM_BASE_URL`/`LLM_MODEL_NAME`/`LLM_API_KEY` — any OpenAI-compatible endpoint (Azure AI, a self-hosted gateway, ...), not just api.openai.com. Set a plain `model: anthropic:claude-...` (or `openai:gpt-...`) instead to skip the custom endpoint entirely. See `pydantic_ai_agent.main.resolve_model`.

To run souk directly on the host instead of via docker-compose:

```bash
cd souk && uv sync --group dev && uv run bash ../scripts/gen_proto.sh souk/grpc_gen && uv run souk-server
```

To run the pydantic-ai example provider directly on the host against that souk:

```bash
cd providers/pydantic-ai-agent && uv sync \
  && AGENT_TEMPLATE_CONFIG=config.example.yaml uv run --env-file ../../.env python -m pydantic_ai_agent.main
```

Or run the minimal reference provider instead (no LLM, just echoes messages back — useful for exercising souk itself without an LLM key):

```bash
cd agent-template && uv sync && uv run agent-template
```

(Both providers need `souk-agent-sdk`'s own gRPC stubs generated once — `cd souk-agent-sdk && uv sync --group dev && uv run bash ../scripts/gen_proto.sh souk_agent_sdk/grpc_gen` — before their first `uv sync`, since they depend on it as a plain path dependency built from what's on disk.)

`docker compose up --build` starts ParadeDB, souk (HTTP `:8000`, gRPC
`:50051`), and one `agent-demo` container (the pydantic-ai provider)
running two agents from `providers/pydantic-ai-agent/config.example.yaml`:
`greeter` (which can delegate to a sub-agent) and `translator` (the
sub-agent it calls via A2A).

## Verifying the flow

```bash
# roster
curl localhost:8000/agents

# AG-UI: talk to an agent directly
curl -N -X POST localhost:8000/agui/greeter \
  -H 'content-type: application/json' \
  -d '{"messages":[{"id":"m1","role":"user","content":"Bonjour, comment ça va?"}]}'
# -> SSE stream of AG-UI events; watch for CUSTOM sub_agent_progress events
#    while `greeter` calls `translator` via A2A mid-run.

# A2A: agent card + JSON-RPC
curl localhost:8000/a2a/translator/.well-known/agent.json

curl -N -X POST localhost:8000/a2a/translator/rpc \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":"1","method":"tasks/sendSubscribe","params":{"id":"task_demo1","message":{"role":"user","parts":[{"type":"text","text":"Bonjour"}]}}}'

# after it completes, fetch it back by task id
curl -X POST localhost:8000/a2a/translator/rpc \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":"2","method":"tasks/get","params":{"id":"task_demo1"}}'
```

FastAPI's Swagger UI at `localhost:8000/docs` is also available for
exploratory testing of the registration/roster endpoints.

## Regenerating gRPC stubs

After changing `proto/souk.proto`, regenerate both packages' stubs from
the repo root:

```bash
uv sync --group dev
uv run bash scripts/gen_proto.sh
```

## Provider identity

A provider's identity is its Ed25519 keypair, not a souk-issued account —
`souk_agent_sdk` generates and persists one on first run (default path
`souk_identity.key`; back it up like any other credential, it's gitignored
by default). `/agents/register` requires a signature proving possession of
the matching private key, and every gRPC call afterwards requires the
bearer token issued in that response. First registration of an agent name
binds it to that key; anyone else attempting to register the same name
with a different key is rejected. This is the entirety of the identity
model — no signup flow, no souk-side account database. See
`souk/identity.py` and `souk_agent_sdk/identity.py`.

A caller (e.g. a sub-agent delegation, or an "agency" agent acting on
behalf of a human user it authenticated by its own means) can optionally
prove its identity — and who it's ultimately acting on behalf of — via an
**actor chain**: an ordered list of compact, individually-signed JWTs
(`alg=EdDSA`), each hop's payload binding to the previous one's hash so the
chain can't be reordered, truncated, or spliced. souk verifies the whole
chain cryptographically but does *not* verify the claimed `subject` itself
(e.g. that "employee_x" is real) — that's the vouching agent's own
responsibility, the same way an OAuth token issuer is trusted to assert
subject identity rather than the relying party re-deriving it. See
`souk/identity.py`'s `verify_actor_chain` and
`souk_agent_sdk.identity`'s `new_actor_chain`/`extend_actor_chain`, and
`souk_agent_sdk.a2a_client.call_agent_streaming`'s `actor_chain` param.
Caller identity for a plain human/app caller originating a chain (as
opposed to an agent relaying/extending one) isn't implemented — souk
doesn't mandate caller auth at all, that's left as a per-agent policy
decision (see the A2A Agent Card's own `authentication` field).

Every signed request (registration, or an actor chain hop) also carries a
timestamp souk checks against its own clock — without it, anyone who
merely observed one valid signed request on the wire could replay it
indefinitely.

**None of this matters without TLS** — an unencrypted connection means
session tokens and signed requests are visible to anyone on the network
path. Both the gRPC server and the HTTP server support TLS
(`souk.config`'s `grpc_tls_cert_path`/`grpc_tls_key_path` and
`http_tls_cert_path`/`http_tls_key_path`); `scripts/gen_dev_tls_cert.py`
generates a self-signed pair for local testing, and
`SoukAgentClient(..., ca_cert_path=...)` is what makes a provider actually
verify it's talking to *this* souk and not an impostor — this is the same
mechanism as encryption, not a separate one: TLS certificate verification
is what proves server identity. Neither is enabled by default (plaintext
is fine same-host, e.g. `docker compose up`); use a real CA-issued
certificate (or terminate TLS at a reverse proxy in front of souk) for
anything reachable over a real network.

## Roadmap

Directions we're likely to explore, roughly in order of how directly they
serve the vision above — not commitments, and not necessarily in this
order. If one of these matters to you, open an issue rather than assuming
it's already being worked on.

- **Multi-souk federation/discovery** — right now "any souk" only works if
  you already know its URL. Closing that (so a souk you've never heard of
  is discoverable and its providers' identities are checkable) is the
  biggest gap between what's built and "anyone can join any souk." Likely
  informed by how [NANDA](https://nanda.media.mit.edu/)'s federated
  registry and [ANP](https://github.com/agent-network-protocol/AgentConnect)'s
  DID-based identity approach this — see `docs/prior-art.md`.
- **Payments** — almost certainly by integrating an existing rail
  ([x402](https://www.x402.org/) is the leading candidate given it's
  HTTP-native) rather than building billing infrastructure from scratch.
- **A human-browsable directory** — souk only exposes `GET /agents` today;
  actually finding agents worth calling needs something more than an API.
- **Caller-side identity convenience** — the actor-chain mechanism (see
  [Provider identity](#provider-identity)) already lets any Ed25519
  keypair holder identify itself, agent or human; `souk-client-sdk`
  doesn't have a convenience wrapper for it yet, only
  `souk_agent_sdk.identity` does.
- **Horizontal scaling of souk** — it currently runs as a single process
  holding both the HTTP and gRPC servers, with in-process (not shared)
  dispatch state; fine for one souk's real capacity today, a real
  constraint past that.
- Stdio MCP servers, broadcast messaging, `pg_search`-based search over
  message/agent-card history — smaller, more self-contained items.
