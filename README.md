# Agent Souk

A **souk** is a public-IP server that agents without a public IP (behind
NAT, running locally, etc.) connect out to and become reachable through.
Once joined, humans or other agents can interact with an agent via the
souk over two protocols:

- **AG-UI** — event-streaming, human/frontend-facing.
- **A2A** — JSON-RPC task protocol, agent-to-agent.

Agents can't be pushed to, so the agent-side SDK polls the souk for
pending work and, once it finds any, opens a gRPC stream to relay AG-UI
events for that run.

## Components

| Path | What it is |
|---|---|
| `proto/souk.proto` | gRPC contract between souk and souk-agent-sdk |
| `souk/` | The gateway server: AG-UI + A2A HTTP surface, gRPC relay, Postgres/ParadeDB persistence |
| `souk-agent-sdk/` | Agent-side SDK: registers a batch of agents, polls for work, relays runs over gRPC, includes a streaming A2A client for calling sub-agents |
| `souk-client-sdk/` | Caller-side SDK: thin AG-UI client for talking to an agent through a souk |
| `agent-template/` | Generic pydantic-ai agent runner: give it a system prompt + MCP servers (+ optional sub-agents) via YAML, it produces a running AG-UI agent wired to a souk |

## Running locally

```bash
cp .env.example .env   # fill in ANTHROPIC_API_KEY (or override each agent's `model:` in config.example.yaml)
docker compose up --build
```

This starts ParadeDB, souk (HTTP `:8000`, gRPC `:50051`), and one
`agent-demo` container running two agents from
`agent-template/config.example.yaml`: `greeter` (which can delegate to a
sub-agent) and `translator` (the sub-agent it calls via A2A).

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

After changing `proto/souk.proto`:

```bash
uv sync --group dev
./scripts/gen_proto.sh
```

## What's deferred beyond v1

Auth/identity verification, stdio MCP servers, multi-souk federation,
broadcast messaging, `pg_search`-based search over message/agent-card
history, and horizontal scaling of souk (it currently runs as a single
process holding both the HTTP and gRPC servers).
