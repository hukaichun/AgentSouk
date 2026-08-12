# Agent Souk 🕌⚡

[![CI](https://github.com/hukaichun/AgentSouk/actions/workflows/ci.yml/badge.svg)](https://github.com/hukaichun/AgentSouk/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Protocol: AG-UI & A2A](https://img.shields.io/badge/Protocols-AG--UI%20%7C%20A2A-blue.svg)](proto/souk.proto)

> **The Zero-Config Open Agent Relay Gateway & Shared Protocol Bridge.**  
> Instantly expose AI agents running anywhere — locally, behind NAT, or in private VPCs — over **AG-UI** (human streaming) and **A2A** (agent-to-agent JSON-RPC) protocols **without public IPs, open ports, tunneling setups (ngrok), or cloud vendor lock-in.**

---

## 💡 What is Agent Souk?

A **souk** (Arabic: سوق) is an open marketplace — a shared rendezvous point where independent vendors set up stalls and anyone can walk in to discover and interact with them.

Traditional souks are famously not curated. The market operator provides the physical space and the footfall; it does not vouch for the quality of every vendor. You recognize a particular stall because it is always in the same spot, run by the same face — but trust in the goods is earned through reputation and direct experience, not through a central authority's seal of approval.

**Agent Souk deliberately follows this model.** It provides:
- **Reachability**: any agent, running anywhere, can connect outbound and become accessible.
- **Verifiable identity**: each provider's Ed25519 keypair is the equivalent of "always the same stall." A caller can cryptographically confirm they are talking to the same key as last time — but Souk itself does not vouch for what that agent *does*.
- **Open access**: any caller can discover and interact with any registered agent without going through an approval gate.

What Souk explicitly does *not* provide is a central reputation layer or quality certification. That is not an oversight — it is the same philosophical choice the souk metaphor implies. Decentralized trust scales; centralized gatekeeping creates the monopoly you were trying to escape.

This makes Agent Souk the right infrastructure for teams, communities, or open ecosystems that want to connect agents across organizational boundaries without ceding control to a single platform — and the wrong fit for contexts that require an operator-curated, closed registry.

That is precisely the architecture of Agent Souk:

```
                           Public Internet
                               │
┌──────────────────────────────▼────────────────────────────────┐
│                   Your Souk (souk.example.com)                 │
│          FastAPI Gateway · gRPC Relay · ParadeDB State        │
└──────┬─────────────────────────────────────────┬──────────────┘
       │ HTTP (AG-UI SSE / A2A JSON-RPC)          │ Outbound-only gRPC streams
       │ any caller can reach                     │ providers connect outbound to
       ▼                                          ▼
 ┌─────────────┐   ┌───────────────┐   ┌──────────────┐   ┌────────────────────┐
 │ Browser /   │   │ External Agent│   │ Alice's Agent│   │ Bob's Agent        │
 │ Web UI      │   │ (A2A Caller)  │   │ (Laptop/NAT) │   │ (Private VPC)      │
 └─────────────┘   └───────────────┘   └──────────────┘   └────────────────────┘
```

Deploying one public instance of Agent Souk creates a **shared relay hub** for your team, organization, or community:
- **For Providers**: AI agents on laptops, local GPUs, edge devices, or private VPCs initiate **outbound-only gRPC persistent streams** to Souk. No open ports, reverse tunnels, or public IP addresses are required.
- **For Callers**: Humans (via the built-in static Web Directory UI or AG-UI clients) and other agents (via A2A RPC) talk directly to Souk's public HTTP surface. Souk routes traffic seamlessly to the connected provider.
- **Zero Lock-in & Independent Host**: Providers execute 100% of their agent logic locally without handing credentials or code to a third-party cloud platform.

### Key Value Propositions

- 🌐 **Zero-Config Ingress & NAT Traversal**: Agents connect *out* to Souk. Zero open ingress ports, static public IPs, or third-party tunnels needed.
- 🔄 **Unified Dual-Protocol Gateway**: Exposes a single HTTP surface bridging human event streaming (**AG-UI**) and machine RPC task delegation (**A2A** / JSON-RPC).
- 🔐 **Cryptographic Self-Sovereign Identity**: Agents own their identity via local **Ed25519 keypairs**. Zero centralized accounts, user databases, or API key friction.
- 🔗 **Auditable Multi-Hop Actor Chains**: Multi-agent delegation embeds tamper-evident, signed JWT EdDSA provenance chains to prevent privilege escalation and trace delegation lineages.
- 💾 **Durable State & Human-in-the-Loop (HITL)**: Backed by Postgres / ParadeDB for persistent threads, execution logs, and native `input-required` async pause & resume flows.
- 🔑 **Keep Your Own Key (KYOK)** *(experimental)*: Privacy relay allowing callers to pay for LLM inference with their own API keys without handing raw credentials to agent hosts.
- 🖥️ **Zero-Backend Directory UI**: Pure static browser client (`souk-directory`) to browse active agents, inspect capabilities, and chat live.

---

## ⚡ Quick Start in 60 Seconds

### Option A: Local Development & Exploration

Spin up the complete stack locally—including the Souk relay gateway, ParadeDB, pre-configured example AI agents (`souk-guide`, `summarizer`, `translator`), and the static Web Directory UI:

```bash
# 1. Clone the repository
git clone https://github.com/hukaichun/AgentSouk.git
cd AgentSouk

# 2. Configure environment variables
cp .env.example .env
# Edit .env to add your LLM credentials (LLM_BASE_URL, LLM_API_KEY, LLM_MODEL_NAME or ANTHROPIC_API_KEY)

# 3. Spin up with Docker Compose
docker compose up --build
```

#### Access Services
- 🌐 **Web Directory & Chat UI**: [`http://localhost:8080`](http://localhost:8080)
- ⚡ **HTTP Gateway / Swagger Docs**: [`http://localhost:8000/docs`](http://localhost:8000/docs)
- 🔌 **gRPC Relay Server**: `localhost:50051`

---

### Option B: Deploying a Public Shared Souk

Deploy Souk to any public server (e.g. `souk.example.com`). Once running, **any provider anywhere in the world** can register an agent using `souk-agent-sdk` without touching their firewall:

```bash
# 1. On your public server (Deploy Gateway & Directory UI):
SOUK_PUBLIC_HTTP_URL=https://souk.example.com \
SOUK_DATABASE_URL=postgresql+psycopg://souk:secret@db:5432/souk \
SOUK_TOKEN_SIGNING_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))") \
SOUK_DB_SCHEMA=souk \
docker compose up -d souk paradedb souk-directory
# `souk` depends on `souk-migrate` completing first (see docker-compose.yml)
# — that's the only step that runs DDL (souk/alembic/), so it's the one
# place to point a DDL-capable DB role; `souk` itself only ever needs DML.
# SOUK_DATABASE_URL and SOUK_TOKEN_SIGNING_SECRET have no built-in default
# — souk.config requires both explicitly, on purpose (see souk/souk/config.py).
# SOUK_DB_SCHEMA is optional (defaults to `public`) — set it to keep souk's
# tables out of `public` when sharing one Postgres instance across services.

# 2. On any remote provider machine (Behind NAT / Firewall):
SOUK_HTTP_URL=https://souk.example.com \
SOUK_GRPC_URL=souk.example.com:50051 \
uv run python -m my_agent
```

Remote agents connect outbound to your Souk hub, and visitors to `https://souk.example.com:8080` can immediately discover and chat with all active agents!

---

## 🏛️ System Architecture

```mermaid
graph TD
    User([Human User / Web Directory]) -->|"POST /agui/{agent} (SSE)"| HTTP[FastAPI HTTP Surface]
    CallerAgent([External Agent / Client]) -->|"POST /a2a/{agent}/rpc (JSON-RPC)"| HTTP

    subgraph SoukServer ["Souk Gateway (Single Process)"]
        HTTP --> Broker[In-Process asyncio Broker]
        GRPC[gRPC Relay Engine] <--> Broker
    end

    SoukServer --> DB[(ParadeDB / Postgres<br/>Roster, Threads, Run History)]

    GRPC <== "Persistent Outbound gRPC Stream<br/>(PollForWork + AgentSession)" ==> SDK[souk-agent-sdk]
    
    SDK --> AgentA["Local Agent A<br/>(Laptop / Behind NAT)"]
    SDK --> AgentB["Enterprise VPC Agent B<br/>(Private Subnet)"]
```

---

## 📊 Competitive Landscape & Positioning

| Feature / Dimension | **Agent Souk** | **a2a-relay** | **agentgateway.dev** | **Cloudflare AI Gateway** | **Google Bedrock / Vertex** |
|---|---|---|---|---|---|
| **NAT Traversal / No Public IP** | ✅ Outbound gRPC stream | ✅ Outbound WebSocket | ❌ Requires reachable backends | ❌ Edge proxy only | ❌ Cloud-hosted backends |
| **Protocol Support** | ✅ Dual AG-UI + A2A | ❌ A2A only | ✅ MCP + A2A + HTTP | ❌ LLM proxy only | ❌ Proprietary / A2A |
| **Identity Model** | 🔐 Ed25519 Keypair | ⚠️ Relay-wide secret token | 🔑 Central OAuth / Cedar RBAC | 🔑 API keys / JWT | 🔒 Cloud IAM / ARNs |
| **Multi-Agent Provenance** | 🔗 Auditable EdDSA Actor Chains | ❌ None | ❌ None | ❌ None | ⚠️ Cloud Audit Logs |
| **State & HITL** | 💾 ParadeDB (Thread DAG & Pause/Resume) | ❌ Stateless forwarder | ❌ Stateless proxy | ❌ Caching only | ⚠️ Managed State |
| **Monetization / Privacy** | 🔑 KYOK (Keep Your Own Key) | ❌ None | ❌ None | 💰 Central billing | 💰 Vendor lock-in |
| **Deployment** | 🐳 Self-hosted / Shared Marketplace | 🐳 Self-hosted | ☸️ K8s / Rust binary | ☁️ Managed SaaS | ☁️ Managed SaaS |

> 📌 **Detailed Comparative Study**: See [`docs/prior-art.md`](docs/prior-art.md) for an in-depth breakdown comparing Agent Souk with adjacent ecosystem projects (A2A relays, agent gateways, DID identity standards, and commercial cloud platforms).

---

## 📦 Repository Structure

The project is structured as modular, independent components:

| Module Path | Description |
|---|---|
| [`proto/souk.proto`](proto/souk.proto) | gRPC contract interface defining `PollForWork` and `AgentSession` |
| [`souk/`](souk/) | Main Gateway Server: FastAPI HTTP endpoints, gRPC relay engine, and ParadeDB persistence |
| [`souk-agent-sdk/`](souk-agent-sdk/) | Python SDK for agent providers: handles registration, polling, streaming, & delegation |
| [`souk-client-sdk/`](souk-client-sdk/) | Python client library for consuming agents over AG-UI & KYOK |
| [`souk-directory/`](souk-directory/) | Zero-backend Web Directory & live Chat UI (compiled TS/ES modules) |
| [`agent-template/`](agent-template/) | Minimal reference agent implementation without LLM dependencies |
| [`providers/`](providers/) | Production-ready examples (Pydantic-AI agent with MCP tools & sub-agent delegation) |
| [`docs/`](docs/) | Deep-dive architectural specs: [`agent-provider-guide.md`](docs/agent-provider-guide.md), [`prior-art.md`](docs/prior-art.md), [`keep-your-own-key.md`](docs/keep-your-own-key.md), and [`federation-and-anti-abuse.md`](docs/federation-and-anti-abuse.md) |

---

## 🛠️ Local Development & Testing

### Direct Host Execution

To run components individually on your host machine using [`uv`](https://github.com/astral-sh/uv):

```bash
# 1. Build proto stubs, apply the schema, & start Souk Gateway Server
cd souk && uv sync --group dev
uv run bash ../scripts/gen_proto.sh souk/grpc_gen
export SOUK_DATABASE_URL=postgresql+psycopg://souk:souk@localhost:5433/souk
export SOUK_TOKEN_SIGNING_SECRET=dev-insecure-change-me  # never reuse this value outside local dev
uv run alembic upgrade head  # one-time DDL step — see souk/alembic/
uv run souk-server

# 2. Run Example Pydantic-AI Provider
cd ../providers/pydantic-ai-agent && uv sync
AGENT_TEMPLATE_CONFIG=config.example.yaml uv run --env-file ../../.env python -m pydantic_ai_agent.main

# Or run the minimal reference provider (no LLM key required):
cd ../../agent-template && uv sync && uv run agent-template
```

> **Note**: For local dev, make sure `souk-agent-sdk` stubs are generated:
> ```bash
> cd souk-agent-sdk && uv sync --group dev && uv run bash ../scripts/gen_proto.sh souk_agent_sdk/grpc_gen
> ```

### Running Tests

Start ParadeDB Postgres via Docker, then execute tests:

```bash
docker compose up paradedb -d
cd souk
SOUK_DATABASE_URL="postgresql+psycopg://souk:souk@localhost:5433/souk" uv run pytest
```

---

## 🧪 API Usage Examples

Once Souk is running, interact directly via `curl`:

```bash
# 1. Roster discovery: List active registered agents
curl http://localhost:8000/agents

# 2. AG-UI: Stream conversation with an agent (SSE format)
curl -N -X POST http://localhost:8000/agui/souk-guide \
  -H 'content-type: application/json' \
  -d '{"messages":[{"id":"m1","role":"user","content":"Hello, what agents are available?"}]}'

# 3. A2A: Inspect Agent Card (.well-known metadata)
curl http://localhost:8000/a2a/translator/.well-known/agent.json

# 4. A2A: Trigger JSON-RPC task delegation
curl -N -X POST http://localhost:8000/a2a/translator/rpc \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":"1","method":"tasks/sendSubscribe","params":{"id":"task_demo1","message":{"role":"user","parts":[{"type":"text","text":"Bonjour"}]}}}'
```

---

## 🔐 Security & Identity Architecture

- **Self-Sovereign Identity**: Agent identity is bound to an **Ed25519 keypair** generated automatically by `souk-agent-sdk`. `/agents/register` verifies cryptographic ownership before issuing short-lived HMAC session bearer tokens.
- **Actor Chain Provenance**: Delegation across multiple agents embeds an **EdDSA signed JWT chain**. Each hop cryptographically binds to the previous hop's SHA-256 hash, preventing token splicing, replay attacks, or impersonation.
- **TLS Security**: Production deployments should enable TLS (supported via `grpc_tls_*` and `http_tls_*` configuration keys in `souk.config`).

---

## 🔮 Roadmap

- 🌐 **Cross-Souk Discovery**: `@souk` addressing (`agent@souk.example.com`) with client-side resolution and a `.well-known/souk-federation.json` discovery document — no inter-souk server-to-server proxying needed. See [`docs/federation-and-anti-abuse.md`](docs/federation-and-anti-abuse.md).
- 🛡️ **Operator Policy Modes**: `open` / `invite-only` / `allowlist` registration modes so each Souk operator decides its own trust model — from public community hub to closed enterprise registry. See [`docs/federation-and-anti-abuse.md`](docs/federation-and-anti-abuse.md).
- 💳 **Native Monetization & Payments**: Integration with micro-payment rails like [x402](https://www.x402.org/) for agent-to-agent transactions and directory-listing economics.
- 📈 **Horizontal Gateway Scaling**: Distributing broker state via Redis / Postgres LISTEN-NOTIFY for multi-replica enterprise deployments.

*Directions, not commitments — the federation/anti-abuse items above are
design proposals only (see that doc's own header), not implemented and
not scheduled. If one of these matters to you, open an issue rather than
assuming it's already underway.*

---

## 🤝 Contributing

We welcome community contributions, suggestions, and pull requests! Please check out [CONTRIBUTING.md](CONTRIBUTING.md) for details on codebase organization and PR workflows.

**License**: [Apache 2.0](LICENSE)
