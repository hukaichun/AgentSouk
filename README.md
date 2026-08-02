# Agent Souk

[![CI](https://github.com/hukaichun/AgentSouk/actions/workflows/ci.yml/badge.svg)](https://github.com/hukaichun/AgentSouk/actions/workflows/ci.yml)

**Agent Souk** is an open, zero-config relay gateway and protocol bridge that makes AI agents—running locally, behind NAT, or on edge devices—instantly reachable over **AG-UI** (human event-streaming) and **A2A** (agent-to-agent JSON-RPC) protocols without requiring public IPs, ingress open ports, or cloud vendor lock-in.

---

## ⚡ Key Features

- **🌐 Zero-Config NAT Traversal**: Outbound-only gRPC persistent streams allow local or firewall-bound agents to become publicly reachable without exposed endpoints or tunneling setups (like ngrok).
- **🔄 Dual Protocol Gateway**: Natively bridges human-facing streaming interfaces (**AG-UI** / SSE) and agent-to-agent task execution (**A2A** / JSON-RPC) on a unified relay.
- **🔐 Self-Sovereign Identity**: Cryptographic Ed25519 keypair authentication ensures verified agent ownership without centralized registration, user databases, or API keys.
- **🔗 Auditable Actor Chains**: Multi-hop JWT EdDSA provenance tracking prevents token tampering and ensures verifiable caller delegation across multi-agent workflows.
- **💾 Durable State & HITL**: Postgres / ParadeDB persistence for thread history, message logging, and Human-in-the-Loop (HITL) async pause & resume support.
- **🖥️ Zero-Backend Web Directory**: Pure browser static client (TypeScript/ES modules) for browsing active agents and chatting live without additional backend services.

---

## 🚀 Quick Start in 1 Minute

Spin up the complete local environment—including the Souk relay gateway, ParadeDB, pre-configured example AI agents (`souk-guide`, `summarizer`, `translator`), and the static Web Directory UI:

```bash
# 1. Prepare environment variables
cp .env.example .env   # fill in LLM_BASE_URL/LLM_MODEL_NAME/LLM_API_KEY (or ANTHROPIC_API_KEY)

# 2. Start the full stack
docker compose up --build
```

Once running:
- 🌐 **Web Directory & Chat UI**: Open [`http://localhost:8080`](http://localhost:8080) to inspect live registered agents and start chatting!
- ⚡ **HTTP Gateway**: `http://localhost:8000` (FastAPI Swagger UI available at `http://localhost:8000/docs`).
- 🔌 **gRPC Relay**: `localhost:50051`.

---

## 🏛️ Vision & Architecture

A **souk** is a public relay layer that agents connect *out* to. Whether deploying internal agents across departments in an enterprise or exposing an agent running on your laptop to a colleague, Souk eliminates reachability friction while keeping agent code and execution 100% self-hosted.

```mermaid
graph TD
    Human([Human / AG-UI client]) -->|"POST /agui/... (SSE)"| HTTP
    Caller([Other agent / A2A caller]) -->|"POST /a2a/.../rpc (JSON-RPC)"| HTTP

    subgraph SoukProcess["souk (single process)"]
        HTTP["HTTP surface — FastAPI"]
        GRPC["gRPC surface — PollForWork / AgentSession"]
        Broker["in-process broker (asyncio)"]
        HTTP --> Broker
        GRPC --> Broker
    end

    SoukProcess --> DB[(Postgres / ParadeDB<br/>roster, threads, run history)]

    GRPC <-->|"outbound-only gRPC<br/>poll + persistent stream"| SDK[souk-agent-sdk]
    SDK --> AgentA[Your Agent A]
    SDK --> AgentB[Your Agent B]
```

### Core Architecture Highlights

- **Outbound-Only Connection**: Neither `AgentA` nor `AgentB` accept inbound network connections. The agent SDK polls for work and opens persistent bidirectional gRPC streams (`AgentSession`) to relay AG-UI & A2A frames.
- **In-Process Broker**: Asyncio dispatch layer routes incoming HTTP requests (AG-UI SSE streams & A2A RPC calls) directly to connected agent gRPC streams.
- **Decoupled Architecture**: Souk executes no agent logic itself—every agent is an independent provider speaking `proto/souk.proto`.

---

## 📦 Project Components

Each component is designed as an independent package within this repository:

| Path | Description |
|---|---|
| [`proto/souk.proto`](file:///home/kai/AgentSouk/proto/souk.proto) | gRPC contract specification between Souk relay server and agent SDKs |
| [`souk/`](file:///home/kai/AgentSouk/souk) | Gateway server: FastAPI HTTP surface (AG-UI + A2A), gRPC relay engine, and ParadeDB persistence |
| [`souk-agent-sdk/`](file:///home/kai/AgentSouk/souk-agent-sdk) | Agent-side Python SDK: handles registration, gRPC work polling, and streaming A2A sub-agent calls |
| [`souk-client-sdk/`](file:///home/kai/AgentSouk/souk-client-sdk) | Caller-side SDK: Client library for interacting with agents over AG-UI protocol |
| [`souk-directory/`](file:///home/kai/AgentSouk/souk-directory) | Zero-backend web directory & chat interface (compiled ES modules, served statically) |
| [`agent-template/`](file:///home/kai/AgentSouk/agent-template) | Minimal reference agent implementation (no LLM framework required) |
| [`providers/`](file:///home/kai/AgentSouk/providers) | Production-ready example providers (Pydantic-AI runner with MCP tool support & A2A delegation) |

---

## 🛠️ Development & Running Locally

### Direct Host Execution

To run the components individually on your host machine:

```bash
# 1. Run Souk Gateway Server
cd souk && uv sync --group dev
uv run bash ../scripts/gen_proto.sh souk/grpc_gen
uv run souk-server

# 2. Run Example Pydantic-AI Provider
cd providers/pydantic-ai-agent && uv sync
AGENT_TEMPLATE_CONFIG=config.example.yaml uv run --env-file ../../.env python -m pydantic_ai_agent.main

# Or run the minimal reference provider (no LLM key required):
cd agent-template && uv sync && uv run agent-template
```

### Running Test Suite

Ensure the ParadeDB Postgres container is running via Docker Compose (`docker compose up -d paradedb`), then run pytest from the `souk/` directory:

```bash
cd souk
SOUK_DATABASE_URL="postgresql+psycopg://souk:souk@localhost:5433/souk" uv run pytest
```

---

## 🧪 Verifying Flow via API

Once the stack is running, you can test the endpoints directly using `curl`:

```bash
# 1. Inspect registered active agents roster
curl localhost:8000/agents

# 2. AG-UI: Talk to an agent directly (SSE stream)
curl -N -X POST localhost:8000/agui/souk-guide \
  -H 'content-type: application/json' \
  -d '{"messages":[{"id":"m1","role":"user","content":"Bonjour, comment ça va?"}]}'

# 3. A2A: Query Agent Card & trigger JSON-RPC tasks
curl localhost:8000/a2a/translator/.well-known/agent.json

curl -N -X POST localhost:8000/a2a/translator/rpc \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":"1","method":"tasks/sendSubscribe","params":{"id":"task_demo1","message":{"role":"user","parts":[{"type":"text","text":"Bonjour"}]}}}'
```

---

## 🔐 Security & Identity Architecture

### Cryptographic Agent Identity
A provider's identity is defined by its **Ed25519 keypair**, created automatically on first run by `souk-agent-sdk`. 
- **Registration Verification**: `/agents/register` requires a signature proving possession of the private key.
- **Session Tokens**: Every gRPC call requires an HMAC bearer token issued upon valid registration.
- **Agent ID Scope**: Re-registering the same name with the same key maintains ownership of the assigned `agent_id`.

### Auditable Actor Chains
For agent-to-agent delegation, callers can supply an **Actor Chain**: an ordered list of compact, individually signed JWTs (`alg=EdDSA`). Each hop binds to the previous hop's SHA-256 hash, creating a tamper-evident chain of provenance to prevent unauthorized token reordering or replay attacks.

For detailed analysis comparing Agent Souk with adjacent ecosystem projects (e.g., A2A relays, agent gateways, DID identity protocols), see [`docs/prior-art.md`](docs/prior-art.md).

---

## 🔮 Roadmap

We are actively advancing Souk toward a federated, decentralized agent ecosystem:

- 🌐 **Multi-Souk Federation & Discovery**: Cross-souk discovery and verifiable registry mechanics based on emerging decentralized identity (DID) standards.
- 💳 **Native Payments & Monetization**: Integration with HTTP-native billing rails like [x402](https://www.x402.org/) for automated agent-to-agent transactions.
- 🧰 **Client Identity SDK Enhancements**: Expanding convenience wrappers for actor-chain signing across non-Python client environments.
- 📈 **Horizontal Gateway Scaling**: Distributing broker state for multi-replica enterprise deployments.

We welcome community contributions, ideas, and pull requests! Feel free to open an issue or join the discussion.
