# Agent Souk

[![CI](https://github.com/hukaichun/AgentSouk/actions/workflows/ci.yml/badge.svg)](https://github.com/hukaichun/AgentSouk/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Protocol: AG-UI & A2A](https://img.shields.io/badge/Protocols-AG--UI%20%7C%20A2A-blue.svg)](docs/integration-contract.md)

**A network-free core library, and the SDKs that speak to it, for making AI agents callable by standard [AG-UI](https://docs.ag-ui.com/) and [A2A](https://a2a-protocol.org/) clients — wherever the agents run.** An agent on a laptop, behind NAT, or in a private VPC connects *outbound* to a souk gateway and becomes reachable; no public IP, no open ingress port, no tunnel service.

---

## What it is

A *souk* is an open market — a shared rendezvous point where independent vendors set up stalls and anyone can walk in. The name is a design stance, unpacked under [Design principles](#design-principles) below.

Concretely, a souk gateway gives you:

- **Reachability without ingress.** Providers hold a persistent outbound stream to the gateway; work is relayed over it. Nothing on the agent's side listens.
- **Two protocols on one HTTP surface.** AG-UI for human-facing event streaming (SSE) and A2A v1.0 for agent-to-agent JSON-RPC. Both wire vocabularies come from the official packages (`ag-ui-protocol`, `a2a-sdk`) — no field name, enum value, or method name is hand-written, so a spec rename fails at import instead of rotting silently.
- **Keypair identity, no accounts.** Each provider generates a local Ed25519 keypair; registration verifies ownership of the key and issues short-lived session tokens. A caller can confirm it is talking to the same key as last time. There is no user database and no central authority vouching for what an agent *does*.
- **Signed delegation chains.** When agents delegate to agents, each hop carries an EdDSA-signed JWT bound to the previous hop's hash — the delegation path is tamper-evident and auditable.
- **Signing, not transport.** What core guarantees is signing and verification (registration, session tokens, actor chains, KYOK's two-part authorization), all bounded by a 60-second freshness window that only helps on an encrypted path — TLS termination, and why it is mandatory off localhost, is the gateway's job and AgentSoukServer's documentation.
- **Durable threads, runs, and human-in-the-loop.** Persistent conversation state with native `input-required` pause and resume. SQLite by default with zero configuration; one `SOUK_DATABASE_URL` switch moves the same code path to Postgres for multi-writer deployments.
- **Keep Your Own Key** *(experimental)*: a relay that lets callers fund LLM inference with their own API keys without handing the raw credential to agent hosts. See [Keep your own key](docs/mechanisms/kyok.md).
- **A static directory UI** (`souk-directory`, in AgentSoukServer) to browse registered agents and chat with them live.

---

## Design principles

A traditional souk is famously not curated. The market operator provides the space and the footfall; it does not vouch for what any stall sells. You recognize a stall because it is always in the same spot, run by the same face — and trust in the goods is earned through reputation and direct experience, not through a central authority's seal of approval. Decentralized trust scales; centralized gatekeeping rebuilds the monopoly you came here to escape.

souk is that market, built as software. The invariants below are load-bearing in the shipped code:

- **The market never runs the stalls.** souk can *ask* an agent to stop; it cannot make it. A run's outcome is recorded only when observed: a run that finishes despite a cancellation request records `completed`, because that is what happened.
- **The same spot, the same face.** Identity is an Ed25519 keypair, not an account — a caller can verify it is talking to the same key as last time, and nobody's seal claims more than that. Sharing a process earns no shortcuts: an in-process provider passes the same registration, identity, and liveness checks a remote one does.
- **The market provides mechanism; the host decides policy.** Open-by-default is souk's own stance, not a constraint it imposes on deployments: an operator who wants an invite-only or allowlisted market puts their own gate in front of `/agents/register` (the reasoning is in [`federation-and-anti-abuse.md`](https://github.com/hukaichun/AgentSouk/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/federation-and-anti-abuse.md), a working note kept in history rather than the tree). The same gateway serves an enterprise-internal deployment and a public one — souk never takes a side between "open" and "curated".
- **Anyone may walk in.** A stock AG-UI or A2A client works unmodified; every souk-invented mechanism is opt-in.
- **The core is network-free — vocabulary included.** No market image for this one; it is a codebase discipline. Which protocol something arrives over is a serving-layer choice, so the core doesn't just avoid importing transports; it avoids *naming* them.

The same principles extend into a conversation-semantics direction that is **designed but not yet implemented** — who may speak on a thread ([responsibility chains](docs/mechanisms/responsibility-chains.md)), when speech gets handled and how to speak to work already in flight ([the two lanes, queueing and interjection](docs/design-records.md#designed-not-built)). Parts of it are under open discussion with the protocol communities: [who answers a delegated `input-required`](https://github.com/a2aproject/A2A/discussions/2148), [the multi-turn gap list](https://github.com/a2aproject/A2A/issues/1992), and [in-flight steering for AG-UI](https://github.com/ag-ui-protocol/ag-ui/issues/2148).

---

## Two repositories, one boundary

The mechanism/policy split runs through the codebase itself, as a hard line between two repos:

| | **AgentSouk** (this repo) | **[AgentSoukServer](https://github.com/hukaichun/AgentSoukServer)** |
|---|---|---|
| **Owns** | The domain: agents, threads, runs, identity, persistence, protocol *translation* | The network: ports, transports, TLS, CORS, endpoints, wire framing, admin surface |
| **Ships** | `souk` (the network-free core library) + [`souk-provider-sdk`](souk-provider-sdk/) and [`souk-llm-provider-sdk`](souk-llm-provider-sdk/) (the two provider-side contracts, also transport-free) | The reference gateway, the transport SDKs (`souk-agent-sdk`, `souk-client-sdk`) and the reference providers |
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

The relay channel's contract is core's provider port — offer / report events / finish, plus a cancel notification — and is deliberately transport-neutral. Today's carrier is a WebSocket on the gateway's one HTTP port (spec: AgentSoukServer's `docs/server-mode.md`); it was a gRPC stream once, and core was untouched by the swap — which is the test of whether this boundary is real.

---

## Quick start (library development)

**Running anything is [AgentSoukServer](https://github.com/hukaichun/AgentSoukServer)'s quick start, not this repo's** — the gateway, the provider and caller SDKs, the reference providers, deployment, Docker, and TLS guidance all live there; it owns both ends of every wire it defines. What lives *here* is the library those wires carry, and it needs no gateway at all:

```bash
cd souk && uv sync --group dev
uv run pytest        # SQLite, zero config
```

The same suite runs against Postgres by pointing at one — dialect bugs only ever appear on one side, so run both before merging (this repo's own `docker compose` carries only a Postgres for exactly this):

```bash
docker compose up paradedb -d
SOUK_DATABASE_URL="postgresql+psycopg://souk:souk@localhost:5433/souk" uv run pytest
```

Schema changes are Alembic revisions packaged under [`souk/souk/alembic/`](souk/souk/alembic/) — `uv run alembic upgrade head`, run from `souk/`, is a deploy-time DDL step, deliberately separate from anything a running gateway does (see `CONTRIBUTING.md`). Deployments that will not let an application migrate its own database have [three supported tiers](docs/core-components/persistence.md).

---

## Where it sits

We know of nothing that occupies this exact intersection — outbound-only reachability, AG-UI and A2A on one gateway, keypair identity with signed delegation, durable state with pause/resume. But each neighbor does its own thing better than souk does:

- **a2a-relay** is a much smaller outbound-WebSocket forwarder. If all you need is A2A passthrough with a shared relay token and no state, it is the simpler tool.
- **[agentgateway.dev](https://agentgateway.dev/)** is a serious ingress data plane — Cedar-based RBAC, observability, MCP support, Kubernetes-grade deployment. Souk has none of that. The trade is that it assumes your backends are already reachable; souk exists for when they are not.
- **Cloudflare AI Gateway** and the cloud agent platforms (**AWS Bedrock AgentCore**, **Google Vertex / Agent Marketplace**) give you managed operations, billing, and SLAs that a self-hosted gateway never will. The trade is that your agents live inside their cloud and their identity model.

The full comparison, including DID standards and MCP tunnels, is in [`prior-art.md`](https://github.com/hukaichun/AgentSouk/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/prior-art.md) — a working note kept in history rather than the tree, so parts of it have aged.

---

## Repository structure

Modular, independent distributions — no shared workspace, each stands alone:

| Module Path | Description |
|---|---|
| [`souk/`](souk/) | **The core library.** Agents, threads, runs, identity, the AG-UI/A2A/KYOK adapters, and SQLite/Postgres persistence. Network-free: it depends on no web framework, no gRPC, no WebSocket library — and cannot, by packaging and by test |
| [`souk-provider-sdk/`](souk-provider-sdk/) | **What an agent provider and souk agree on**, from the provider's side: identity and what it signs, the port an agent implements, and the provider's own worker loop. Carries no transport — its dependencies are `cryptography`, `pyjwt` and `ag-ui-protocol` (the event vocabulary), and not `souk`. See its [README](souk-provider-sdk/README.md) |
| [`souk-llm-provider-sdk/`](souk-llm-provider-sdk/) | **What an LLM provider and souk agree on** — the party answering KYOK completions: the port, the delivered-completion envelope, the structured refusal, its own registration payloads. Depends on `souk-provider-sdk` (identity is shared) and `openai` (types only), not `souk`. See its [README](souk-llm-provider-sdk/README.md) |
| [`docs/`](docs/) | The published site — the [integration contract](docs/integration-contract.md), souk's mechanisms, core components, the SDKs, [writing a transport](docs/writing-a-transport.md), the [design records](docs/design-records.md) (why souk is shaped this way, including the shapes it had first), and [`contract-vectors.json`](docs/contract-vectors.json), the byte-level authority behind them |

Several names circulate and they are different packages: **`souk-provider-sdk` and `souk-llm-provider-sdk` are here** and define the interaction; `souk-agent-sdk` (a client for the gateway's provider WebSocket) and `souk-client-sdk` (the caller's side) live in [AgentSoukServer](https://github.com/hukaichun/AgentSoukServer), along with the reference providers (`agent-template`, `providers/*`) and the directory UI (`souk-directory`). The gateway repo owns both ends of every wire it defines, and the clients and examples live with the stack they front.

---

## Roadmap

- **WebSocket relay base mode**: one gateway port for callers *and* providers — landed. Spec, serving implementation and the SDK transports all live in AgentSoukServer ([`docs/server-mode.md`](https://github.com/hukaichun/AgentSoukServer/blob/main/docs/server-mode.md)); the gRPC carrier and its `proto/souk.proto` are retired.
- **Cross-souk discovery**: `@souk` addressing (`agent@souk.example.com`) with client-side resolution and a `.well-known/souk-federation.json` discovery document — no inter-souk server-to-server proxying needed. See [`federation-and-anti-abuse.md`](https://github.com/hukaichun/AgentSouk/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/federation-and-anti-abuse.md).
- **Native monetization and payments**: integration with micro-payment rails like [x402](https://www.x402.org/) for agent-to-agent transactions and directory-listing economics.
- **Horizontal gateway scaling**: distributing broker state via Redis / Postgres LISTEN-NOTIFY for multi-replica deployments. The measured baseline and the lease design are in [`broker-horizontal-scaling.md`](https://github.com/hukaichun/AgentSouk/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/broker-horizontal-scaling.md), a working note kept in history; `scripts/probes/node.py` and `probe_multiprocess.py` are built on it.
- **Public key revocation**: a `revoked_keys` blocklist checked at registration and actor-chain verification, so a leaked provider/caller Ed25519 key can be shut out immediately instead of waiting for its holder to stop using it. Written to only by whoever already has direct DB access — no new admin/auth surface inside souk itself, consistent with souk having no account system at all.

*Directions, not commitments — the federation/anti-abuse items above are
design proposals only (see that doc's own header), not implemented and
not scheduled. If one of these matters to you, open an issue rather than
assuming it's already underway.*

---

## Contributing

Suggestions, issues, and pull requests are welcome — [CONTRIBUTING.md](CONTRIBUTING.md) covers codebase organization and the PR workflow.

**License**: [Apache 2.0](LICENSE)
