# Agent Souk

[![CI](https://github.com/hukaichun/AgentSouk/actions/workflows/ci.yml/badge.svg)](https://github.com/hukaichun/AgentSouk/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Protocol: AG-UI & A2A](https://img.shields.io/badge/Protocols-AG--UI%20%7C%20A2A-blue.svg)](docs/library-architecture.md)

**A network-free core library, and the SDKs that speak to it, for making AI agents callable by standard [AG-UI](https://docs.ag-ui.com/) and [A2A](https://a2a-protocol.org/) clients — wherever the agents run.** An agent on a laptop, behind NAT, or in a private VPC connects *outbound* to a souk gateway and becomes reachable; no public IP, no open ingress port, no tunnel service.

---

## What it is

A *souk* is an open market: the operator provides the space and the footfall; vendors keep their own stall, face, and reputation. The name marks the design stance, which is load-bearing throughout the code: **souk provides mechanism; whoever hosts an instance decides policy.** An operator who wants an invite-only or allowlisted registry puts their own logic in front of souk's `/agents/register` endpoint rather than souk deciding on their behalf (see [`docs/federation-and-anti-abuse.md`](docs/federation-and-anti-abuse.md)). Souk itself never takes a side between "open" and "curated" — the same gateway serves an enterprise-internal deployment and a public one.

Concretely, a souk gateway gives you:

- **Reachability without ingress.** Providers hold a persistent outbound stream to the gateway; work is relayed over it. Nothing on the agent's side listens.
- **Two protocols on one HTTP surface.** AG-UI for human-facing event streaming (SSE) and A2A v1.0 for agent-to-agent JSON-RPC. Both wire vocabularies come from the official packages (`ag-ui-protocol`, `a2a-sdk`) — no field name, enum value, or method name is hand-written, so a spec rename fails at import instead of rotting silently.
- **Keypair identity, no accounts.** Each provider generates a local Ed25519 keypair; registration verifies ownership of the key and issues short-lived session tokens. A caller can confirm it is talking to the same key as last time. There is no user database and no central authority vouching for what an agent *does*.
- **Signed delegation chains.** When agents delegate to agents, each hop carries an EdDSA-signed JWT bound to the previous hop's hash — the delegation path is tamper-evident and auditable.
- **Durable threads, runs, and human-in-the-loop.** Persistent conversation state with native `input-required` pause and resume. SQLite by default with zero configuration; one `SOUK_DATABASE_URL` switch moves the same code path to Postgres for multi-writer deployments.
- **Keep Your Own Key** *(experimental)*: a relay that lets callers fund LLM inference with their own API keys without handing the raw credential to agent hosts. See [`docs/keep-your-own-key.md`](docs/keep-your-own-key.md).
- **A static directory UI** (`souk-directory`, in AgentSoukServer) to browse registered agents and chat with them live.

---

## Design principles

Four invariants are load-bearing in the shipped code — every one has caused a real bug when bent:

- **souk never decides on a provider's behalf.** It can *ask* an agent to stop; it cannot make it. A run's outcome is recorded only when observed: a run that finishes despite a cancellation request records `completed`, because that is what happened.
- **The core is network-free — vocabulary included.** Which protocol something arrives over is a serving-layer choice, so the core doesn't just avoid importing transports; it avoids *naming* them.
- **Identity is a keypair, not an account.** And sharing a process earns no shortcuts: an in-process provider passes the same registration, identity, and liveness checks a remote one does.
- **Standard clients, unmodified.** A stock AG-UI or A2A client works as-is; every souk-invented mechanism is opt-in.

The same principles extend into a conversation-semantics direction that is **designed but not yet implemented** — who may speak on a thread ([`docs/responsibility-chains.md`](docs/responsibility-chains.md)), when speech gets handled and how to speak to work already in flight ([`docs/conversation-semantics.md`](docs/conversation-semantics.md)). Parts of it are under open discussion with the protocol communities: [who answers a delegated `input-required`](https://github.com/a2aproject/A2A/discussions/2148), [the multi-turn gap list](https://github.com/a2aproject/A2A/issues/1992), and [in-flight steering for AG-UI](https://github.com/ag-ui-protocol/ag-ui/issues/2148).

---

## Two repositories, one boundary

The mechanism/policy split runs through the codebase itself, as a hard line between two repos:

| | **AgentSouk** (this repo) | **[AgentSoukServer](https://github.com/hukaichun/AgentSoukServer)** |
|---|---|---|
| **Owns** | The domain: agents, threads, runs, identity, persistence, protocol *translation* | The network: ports, transports, TLS, CORS, endpoints, wire framing, admin surface |
| **Ships** | `souk` (the network-free core library) + [`souk-provider-sdk`](souk-provider-sdk/) (the provider-side contract, also transport-free) | The reference gateway, the transport SDKs (`souk-agent-sdk`, `souk-client-sdk`) and the reference providers |
| **May it bind a socket?** | Never. `souk` cannot even *import* a transport — enforced by packaging and by `souk/tests/test_core_is_network_free.py` | Yes — that is its entire job |

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

The relay channel's contract is core's worker port — `claim_work` / `report_event` / `finish_run` plus a cancel notification — and is deliberately transport-neutral. Today's carrier is a gRPC stream; the base server mode moves it to a WebSocket on the gateway's one HTTP port (spec: AgentSoukServer's `docs/server-mode.md`). Core is untouched by that change, which is the test of whether this boundary is real.

---

## Quick start

**Running anything is [AgentSoukServer](https://github.com/hukaichun/AgentSoukServer)'s quick start, not this repo's** — the gateway, the provider and caller SDKs, the reference providers (`agent-template`, `providers/*`), deployment, Docker, TLS, and configuration guidance all live there; it owns both ends of every wire it defines. What lives *here* is the library those wires carry: the domain, its persistence, and the protocol translation.

```bash
# Library development is the whole quick start:
cd souk && uv sync --group dev
uv run pytest        # SQLite, zero config
```

The full local demo stack (gateway + database + example agents + directory UI) lives with the gateway, in AgentSoukServer — this repo's own `docker compose` carries only a Postgres for running the test suites.

---

## Where it sits

We know of nothing that occupies this exact intersection — outbound-only reachability, AG-UI and A2A on one gateway, keypair identity with signed delegation, durable state with pause/resume. But each neighbor does its own thing better than souk does:

- **a2a-relay** is a much smaller outbound-WebSocket forwarder. If all you need is A2A passthrough with a shared relay token and no state, it is the simpler tool.
- **[agentgateway.dev](https://agentgateway.dev/)** is a serious ingress data plane — Cedar-based RBAC, observability, MCP support, Kubernetes-grade deployment. Souk has none of that. The trade is that it assumes your backends are already reachable; souk exists for when they are not.
- **Cloudflare AI Gateway** and the cloud agent platforms (**AWS Bedrock AgentCore**, **Google Vertex / Agent Marketplace**) give you managed operations, billing, and SLAs that a self-hosted gateway never will. The trade is that your agents live inside their cloud and their identity model.

The full comparison, including DID standards and MCP tunnels, is in [`docs/prior-art.md`](docs/prior-art.md).

---

## System architecture

```mermaid
graph TD
    User([Human User / Web Directory]) -->|"POST /agui/{agent} (SSE)"| HTTP[Gateway HTTP Surface]
    CallerAgent([External Agent / Client]) -->|"POST /a2a/{agent}/rpc (JSON-RPC)"| HTTP

    subgraph Gateway ["Souk gateway (AgentSoukServer)"]
        HTTP --> Core["souk core<br/>(broker, handlers, protocol adapters)"]
        Relay["Relay engine<br/>(outbound persistent streams)"] <--> Core
    end

    Gateway --> DB[(SQLite / Postgres<br/>Roster, Threads, Run History)]

    Relay <== "claim work / report events / finish<br/>+ cancel back the other way" ==> SDK["souk-agent-sdk<br/>(in AgentSoukServer)"]

    SDK --> AgentA["Local Agent A<br/>(Laptop / Behind NAT)"]
    SDK --> AgentB["Enterprise VPC Agent B<br/>(Private Subnet)"]
```

---

## Repository structure

Modular, independent distributions — no shared workspace, each stands alone:

| Module Path | Description |
|---|---|
| [`souk/`](souk/) | **The core library.** Agents, threads, runs, identity, the AG-UI/A2A/KYOK adapters, and SQLite/Postgres persistence. Network-free: it depends on no web framework, no gRPC, no WebSocket library — and cannot, by packaging and by test |
| [`souk-provider-sdk/`](souk-provider-sdk/) | **What a provider and souk agree on**, from the provider's side: identity and what it signs, the port an agent implements, and the provider's own worker loop. Carries no transport — its dependencies are `cryptography` and `pyjwt`, and not `souk` either. See its [README](souk-provider-sdk/README.md) |
| [`docs/`](docs/) | The design record: [`library-architecture.md`](docs/library-architecture.md) (the core/serving split and every decision behind it), [`agent-provider-guide.md`](docs/agent-provider-guide.md), [`keep-your-own-key.md`](docs/keep-your-own-key.md), [`responsibility-chains.md`](docs/responsibility-chains.md), [`conversation-semantics.md`](docs/conversation-semantics.md), [`federation-and-anti-abuse.md`](docs/federation-and-anti-abuse.md), [`prior-art.md`](docs/prior-art.md) |

Three names circulate and they are different packages: **`souk-provider-sdk` is here** and defines the interaction; `souk-agent-sdk` (a client for the gateway's provider WebSocket) and `souk-client-sdk` (the caller's side) live in [AgentSoukServer](https://github.com/hukaichun/AgentSoukServer), along with the reference providers (`agent-template`, `providers/*`) and the directory UI (`souk-directory`). The gateway repo owns both ends of every wire it defines, and the clients and examples live with the stack they front.

---

## Local development and testing

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

## API usage examples

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

## Security and identity

- **Keypair identity**: agent identity is bound to an Ed25519 keypair generated locally by the provider SDK (in AgentSoukServer). `/agents/register` verifies cryptographic ownership before issuing short-lived HMAC session bearer tokens. No accounts, no central user database.
- **Actor chain provenance**: delegation across multiple agents embeds an EdDSA-signed JWT chain; each hop cryptographically binds to the previous hop's SHA-256 hash, preventing token splicing, replay, or impersonation.
- **Transport security is the gateway's job**: what core guarantees is signing and verification (registration signatures, session tokens, actor chains, KYOK's two-part authorization) — all bounded by a 60s freshness window that only helps on an encrypted path. TLS termination, and why it is mandatory off localhost, is documented in AgentSoukServer's README.

---

## Roadmap

- **WebSocket relay base mode**: one gateway port for callers *and* providers — landed. Spec, serving implementation and the SDK transports all live in AgentSoukServer ([`docs/server-mode.md`](https://github.com/hukaichun/AgentSoukServer/blob/main/docs/server-mode.md)); the gRPC carrier and its `proto/souk.proto` are retired.
- **Cross-souk discovery**: `@souk` addressing (`agent@souk.example.com`) with client-side resolution and a `.well-known/souk-federation.json` discovery document — no inter-souk server-to-server proxying needed. See [`docs/federation-and-anti-abuse.md`](docs/federation-and-anti-abuse.md).
- **Native monetization and payments**: integration with micro-payment rails like [x402](https://www.x402.org/) for agent-to-agent transactions and directory-listing economics.
- **Horizontal gateway scaling**: distributing broker state via Redis / Postgres LISTEN-NOTIFY for multi-replica deployments — see `docs/library-architecture.md`'s "What this leaves open" for what two replicas actually do today.
- **Public key revocation**: a `revoked_keys` blocklist checked at registration and actor-chain verification, so a leaked provider/caller Ed25519 key can be shut out immediately instead of waiting for its holder to stop using it. Written to only by whoever already has direct DB access — no new admin/auth surface inside souk itself, consistent with souk having no account system at all.

*Directions, not commitments — the federation/anti-abuse items above are
design proposals only (see that doc's own header), not implemented and
not scheduled. If one of these matters to you, open an issue rather than
assuming it's already underway.*

---

## Contributing

Suggestions, issues, and pull requests are welcome — [CONTRIBUTING.md](CONTRIBUTING.md) covers codebase organization and the PR workflow.

**License**: [Apache 2.0](LICENSE)
