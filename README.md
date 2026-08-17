# Agent Souk 🕌⚡

[![CI](https://github.com/hukaichun/AgentSouk/actions/workflows/ci.yml/badge.svg)](https://github.com/hukaichun/AgentSouk/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Protocol: AG-UI & A2A](https://img.shields.io/badge/Protocols-AG--UI%20%7C%20A2A-blue.svg)](docs/library-architecture.md)

> **The Open Agent Relay: a network-free core library, and the SDKs that speak to it.**  
> Instantly expose AI agents running anywhere — locally, behind NAT, or in private VPCs — over **AG-UI** (human streaming) and **A2A** (agent-to-agent JSON-RPC) **without public IPs, open ports, tunneling setups (ngrok), or cloud vendor lock-in.**

---

## 💡 What is Agent Souk?

A **souk** (Arabic: سوق) is an open marketplace — a shared rendezvous point where independent vendors set up stalls and anyone can walk in to discover and interact with them.

Traditional souks are famously not curated. The market operator provides the physical space and the footfall; it does not vouch for the quality of every vendor. You recognize a particular stall because it is always in the same spot, run by the same face — but trust in the goods is earned through reputation and direct experience, not through a central authority's seal of approval.

**Agent Souk deliberately follows this model.** It provides:
- **Reachability**: any agent, running anywhere, can connect outbound and become accessible.
- **Verifiable identity**: each provider's Ed25519 keypair is the equivalent of "always the same stall." A caller can cryptographically confirm they are talking to the same key as last time — but Souk itself does not vouch for what that agent *does*.
- **Open access**: any caller can discover and interact with any registered agent without going through an approval gate.

What Souk explicitly does *not* provide is a central reputation layer or quality certification. That is not an oversight — it is the same philosophical choice the souk metaphor implies. Decentralized trust scales; centralized gatekeeping creates the monopoly you were trying to escape.

**Souk provides mechanism; whoever hosts an instance decides policy.** Open-by-default is Souk's own stance, not a constraint it imposes on every deployment — an operator who wants an invite-only or allowlisted registry puts their own logic in front of Souk's `/agents/register` endpoint rather than Souk deciding on their behalf (see [`docs/federation-and-anti-abuse.md`](docs/federation-and-anti-abuse.md)). Souk itself never takes a side between "open" and "curated" — it stays out of that decision either way.

---

## 🧭 Two Repositories, One Boundary

The same mechanism/policy split runs through the codebase itself, as a hard line between two repos:

| | **AgentSouk** (this repo) | **[AgentSoukServer](https://github.com/hukaichun/AgentSoukServer)** |
|---|---|---|
| **Owns** | The domain: agents, threads, runs, identity, persistence, protocol *translation* | The network: ports, transports, TLS, CORS, endpoints, wire framing, admin surface |
| **Ships** | `souk` (the network-free core library) + [`souk-provider-sdk`](souk-provider-sdk/) (the provider-side contract, also transport-free) | The reference gateway, the transport SDKs (`souk-agent-sdk`, `souk-client-sdk`) and the reference providers |
| **May it bind a socket?** | ❌ Never. `souk` cannot even *import* a transport — enforced by packaging and by `souk/tests/test_core_is_network_free.py` | ✅ That is its entire job |

Three consequences, recorded in [AgentSouk#27](https://github.com/hukaichun/AgentSouk/issues/27) and load-bearing:

- **No network design originates here.** When a need looks network-shaped, it becomes a core mechanism *plus* a serving decision made downstream — never a new endpoint, transport, or subproject in this repo.
- **The wire contract is authored downstream.** AgentSoukServer's [`docs/server-mode.md`](https://github.com/hukaichun/AgentSoukServer/blob/main/docs/server-mode.md) is the spec of record (single HTTP port; WebSocket relays for providers and KYOK bridges). The SDKs here *implement* that contract; they do not define it.
- **Core's own vocabulary is transport-neutral.** A worker "claims work and reports events" — whether a gRPC stream, a WebSocket, or an in-process call carries that is not core's business, and `websockets` sits in the forbidden-imports list ahead of anything importing it.

That is precisely the runtime architecture:

```
                           Public Internet
                               │
┌──────────────────────────────▼────────────────────────────────┐
│              Your Souk gateway (AgentSoukServer)               │
│        one HTTP surface · relay engine · souk core inside      │
│                SQLite / Postgres durable state                 │
└──────┬─────────────────────────────────────────┬──────────────┘
       │ HTTP (AG-UI SSE / A2A JSON-RPC)          │ Outbound-only persistent streams
       │ any caller can reach                     │ providers connect outbound to
       ▼                                          ▼
 ┌─────────────┐   ┌───────────────┐   ┌──────────────┐   ┌────────────────────┐
 │ Browser /   │   │ External Agent│   │ Alice's Agent│   │ Bob's Agent        │
 │ Web UI      │   │ (A2A Caller)  │   │ (Laptop/NAT) │   │ (Private VPC)      │
 └─────────────┘   └───────────────┘   └──────────────┘   └────────────────────┘
```

### Key Value Propositions

- 🌐 **Zero-Config Ingress & NAT Traversal**: Agents connect *out* to a souk. Zero open ingress ports, static public IPs, or third-party tunnels needed.
- 🔄 **Unified Dual-Protocol Gateway**: One HTTP surface bridging human event streaming (**AG-UI**) and machine RPC task delegation (**A2A v1.0**, shapes taken from the official `a2a-sdk` rather than hand-written).
- 🔐 **Cryptographic Self-Sovereign Identity**: Agents own their identity via local **Ed25519 keypairs**. Zero centralized accounts, user databases, or API key friction.
- 🔗 **Auditable Multi-Hop Actor Chains**: Multi-agent delegation embeds tamper-evident, signed JWT EdDSA provenance chains to prevent privilege escalation and trace delegation lineages.
- 💾 **Durable State & Human-in-the-Loop (HITL)**: Persistent threads, execution logs, and native `input-required` async pause & resume flows — on a zero-config local **SQLite** file by default, or **Postgres / ParadeDB** for a concurrent multi-writer deployment (one `SOUK_DATABASE_URL` switch, no code change).
- 🔑 **Keep Your Own Key (KYOK)** *(experimental)*: Privacy relay allowing callers to pay for LLM inference with their own API keys without handing raw credentials to agent hosts.
- 🖥️ **Zero-Backend Directory UI**: Pure static browser client (`souk-directory`, in AgentSoukServer) to browse active agents, inspect capabilities, and chat live.

---

## ⚡ Quick Start

**Running anything is [AgentSoukServer](https://github.com/hukaichun/AgentSoukServer)'s quick start, not this repo's** — the gateway, the provider and caller SDKs, the reference providers (`agent-template`, `providers/*`), deployment, Docker, TLS, and configuration guidance all live there; it owns both ends of every wire it defines. What lives *here* is the library those wires carry: the domain, its persistence, and the protocol translation.

```bash
# Library development is the whole quick start:
cd souk && uv sync --group dev
uv run pytest        # SQLite, zero config
```

The full local demo stack (gateway + database + example agents + directory UI) lives with the gateway, in AgentSoukServer — this repo's own `docker compose` carries only a Postgres for running the test suites.

---

## 🏛️ System Architecture

```mermaid
graph TD
    User([Human User / Web Directory]) -->|"POST /agui/{agent} (SSE)"| HTTP[Gateway HTTP Surface]
    CallerAgent([External Agent / Client]) -->|"POST /a2a/{agent}/rpc (JSON-RPC)"| HTTP

    subgraph Gateway ["Souk gateway (AgentSoukServer)"]
        HTTP --> Core["souk core<br/>(broker, handlers, protocol adapters)"]
        Relay["Relay engine<br/>(outbound persistent streams)"] <--> Core
    end

    Gateway --> DB[(SQLite / Postgres<br/>Roster, Threads, Run History)]

    Relay <== "claim work / report events / finish<br/>+ cancel back the other way" ==> SDK[souk-agent-sdk<br/>(in AgentSoukServer)]

    SDK --> AgentA["Local Agent A<br/>(Laptop / Behind NAT)"]
    SDK --> AgentB["Enterprise VPC Agent B<br/>(Private Subnet)"]
```

The relay channel's contract is core's worker port — `claim_work` / `report_event` / `finish_run` plus a cancel notification — and is deliberately transport-neutral. Today's carrier is a gRPC stream; the base server mode moves it to a WebSocket on the gateway's one HTTP port (spec: AgentSoukServer's `docs/server-mode.md`). Core is untouched by that change, which is the test of whether this boundary is real.

---

## 📊 Competitive Landscape & Positioning

| Feature / Dimension | **Agent Souk** | **a2a-relay** | **agentgateway.dev** | **Cloudflare AI Gateway** | **Google Bedrock / Vertex** |
|---|---|---|---|---|---|
| **NAT Traversal / No Public IP** | ✅ Outbound persistent stream | ✅ Outbound WebSocket | ❌ Requires reachable backends | ❌ Edge proxy only | ❌ Cloud-hosted backends |
| **Protocol Support** | ✅ Dual AG-UI + A2A | ❌ A2A only | ✅ MCP + A2A + HTTP | ❌ LLM proxy only | ❌ Proprietary / A2A |
| **Identity Model** | 🔐 Ed25519 Keypair | ⚠️ Relay-wide secret token | 🔑 Central OAuth / Cedar RBAC | 🔑 API keys / JWT | 🔒 Cloud IAM / ARNs |
| **Multi-Agent Provenance** | 🔗 Auditable EdDSA Actor Chains | ❌ None | ❌ None | ❌ None | ⚠️ Cloud Audit Logs |
| **State & HITL** | 💾 ParadeDB (Thread DAG & Pause/Resume) | ❌ Stateless forwarder | ❌ Stateless proxy | ❌ Caching only | ⚠️ Managed State |
| **Monetization / Privacy** | 🔑 KYOK (Keep Your Own Key) | ❌ None | ❌ None | 💰 Central billing | 💰 Vendor lock-in |
| **Deployment** | 🐳 Self-hosted / Shared Marketplace | 🐳 Self-hosted | ☸️ K8s / Rust binary | ☁️ Managed SaaS | ☁️ Managed SaaS |

> 📌 **Detailed Comparative Study**: See [`docs/prior-art.md`](docs/prior-art.md) for an in-depth breakdown comparing Agent Souk with adjacent ecosystem projects (A2A relays, agent gateways, DID identity standards, and commercial cloud platforms).

---

## 📦 Repository Structure

Modular, independent distributions — no shared workspace, each stands alone:

| Module Path | Description |
|---|---|
| [`souk/`](souk/) | **The core library.** Agents, threads, runs, identity, the AG-UI/A2A/KYOK adapters, and SQLite/Postgres persistence. Network-free: it depends on no web framework, no gRPC, no WebSocket library — and cannot, by packaging and by test |
| [`souk-provider-sdk/`](souk-provider-sdk/) | **What a provider and souk agree on**, from the provider's side: identity and what it signs, the port an agent implements, and the provider's own worker loop. Carries no transport — its dependencies are `cryptography` and `pyjwt`, and not `souk` either. See its [README](souk-provider-sdk/README.md) |
| [`docs/`](docs/) | The design record: [`library-architecture.md`](docs/library-architecture.md) (the core/serving split and every decision behind it), [`agent-provider-guide.md`](docs/agent-provider-guide.md), [`keep-your-own-key.md`](docs/keep-your-own-key.md), [`federation-and-anti-abuse.md`](docs/federation-and-anti-abuse.md), [`prior-art.md`](docs/prior-art.md) |

Three names circulate and they are different packages: **`souk-provider-sdk` is here** and defines the interaction; `souk-agent-sdk` (a client for the gateway's provider WebSocket) and `souk-client-sdk` (the caller's side) live in [AgentSoukServer](https://github.com/hukaichun/AgentSoukServer), along with the reference providers (`agent-template`, `providers/*`) and the directory UI (`souk-directory`). The gateway repo owns both ends of every wire it defines, and the clients and examples live with the stack they front.

---

## 🛠️ Local Development & Testing

Library development needs no gateway at all — `souk`'s suite runs against the core directly:

```bash
cd souk && uv sync --group dev
uv run pytest                      # SQLite, zero configuration
```

The same suite runs against Postgres by pointing at one (dialect bugs only ever appear on one side — run both before merging):

```bash
docker compose up paradedb -d
SOUK_DATABASE_URL="postgresql+psycopg://souk:souk@localhost:5433/souk" uv run pytest
```

Schema changes are Alembic revisions under [`souk/alembic/`](souk/alembic/) — `uv run alembic upgrade head` is a deploy-time DDL step, deliberately separate from anything a running gateway does (see `CONTRIBUTING.md`).

To see code running against a real gateway, use a checkout of [AgentSoukServer](https://github.com/hukaichun/AgentSoukServer) — its README is the gateway quick start.

---

## 🧪 API Usage Examples

Against any running souk gateway:

```bash
# 1. Roster discovery: List active registered agents
curl http://localhost:8000/agents

# 2. AG-UI: Stream conversation with an agent (SSE format)
curl -N -X POST http://localhost:8000/agui/souk-guide \
  -H 'content-type: application/json' \
  -d '{"messages":[{"id":"m1","role":"user","content":"Hello, what agents are available?"}]}'

# 3. A2A: Inspect Agent Card (the v1.0 well-known path — the only one served;
#    a pre-v1 client would get a v1.0 body it cannot read, so the old path 404s)
curl http://localhost:8000/a2a/translator/.well-known/agent-card.json

# 4. A2A: Trigger JSON-RPC task delegation
curl -N -X POST http://localhost:8000/a2a/translator/rpc \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":"1","method":"SendStreamingMessage","params":{"message":{"role":"ROLE_USER","parts":[{"text":"Bonjour"}]}}}'
```

---

## 🔐 Security & Identity Architecture

- **Self-Sovereign Identity**: Agent identity is bound to an **Ed25519 keypair** generated automatically by the provider SDK (in AgentSoukServer). `/agents/register` verifies cryptographic ownership before issuing short-lived HMAC session bearer tokens.
- **Actor Chain Provenance**: Delegation across multiple agents embeds an **EdDSA signed JWT chain**. Each hop cryptographically binds to the previous hop's SHA-256 hash, preventing token splicing, replay attacks, or impersonation.
- **Transport security is the gateway's job**: what core guarantees is signing and verification (registration signatures, session tokens, actor chains, KYOK's two-part authorization) — all bounded by a 60s freshness window that only helps on an encrypted path. TLS termination, and why it is mandatory off localhost, is documented in AgentSoukServer's README.

---

## 🔮 Roadmap

- 🔌 **WebSocket relay base mode**: one gateway port for callers *and* providers — landed. Spec, serving implementation and the SDK transports all live in AgentSoukServer ([`docs/server-mode.md`](https://github.com/hukaichun/AgentSoukServer/blob/main/docs/server-mode.md)); the gRPC carrier and its `proto/souk.proto` are retired.
- 🌐 **Cross-Souk Discovery**: `@souk` addressing (`agent@souk.example.com`) with client-side resolution and a `.well-known/souk-federation.json` discovery document — no inter-souk server-to-server proxying needed. See [`docs/federation-and-anti-abuse.md`](docs/federation-and-anti-abuse.md).
- 💳 **Native Monetization & Payments**: Integration with micro-payment rails like [x402](https://www.x402.org/) for agent-to-agent transactions and directory-listing economics.
- 📈 **Horizontal Gateway Scaling**: Distributing broker state via Redis / Postgres LISTEN-NOTIFY for multi-replica deployments — see `docs/library-architecture.md`'s "What this leaves open" for what two replicas actually do today.
- 🔑 **Public Key Revocation**: A `revoked_keys` blocklist checked at registration and actor-chain verification, so a leaked provider/caller Ed25519 key can be shut out immediately instead of waiting for its holder to stop using it. Written to only by whoever already has direct DB access — no new admin/auth surface inside souk itself, consistent with souk having no account system at all.

*Directions, not commitments — the federation/anti-abuse items above are
design proposals only (see that doc's own header), not implemented and
not scheduled. If one of these matters to you, open an issue rather than
assuming it's already underway.*

---

## 🤝 Contributing

We welcome community contributions, suggestions, and pull requests! Please check out [CONTRIBUTING.md](CONTRIBUTING.md) for details on codebase organization and PR workflows.

**License**: [Apache 2.0](LICENSE)
