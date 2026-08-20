# Prior Art & Comparative Analysis

This document provides a technical comparison between **Agent Souk** and existing/emerging projects across the AI agent networking, gateway, and identity ecosystem.

> ℹ️ *This is an evolving architectural comparison based on public specifications, code repositories, and documentation. If you notice any outdated or missing information, please open an issue or PR.*

---

## 🎯 The Core Problem & Intersection

**Agent Souk** was designed to solve a specific convergence of challenges that single-purpose projects usually address in isolation:

1. **NAT Traversal / Reachability**: Running agents locally or behind firewalls without public IP addresses, exposed ingress ports, or third-party tunnels (like ngrok).
2. **Dual-Protocol Gateway**: Bridging human event streaming (**AG-UI** / SSE) and agent-to-agent task execution (**A2A** / JSON-RPC) on a single unified relay.
3. **Self-Sovereign Identity & Provenance**: Zero-signup Ed25519 identity keypairs with multi-hop cryptographic actor chains for auditable agent delegation.
4. **Durable State & HITL**: Persistent thread history, DAG lineage, and native Human-in-the-Loop (`input-required`) pause & resume states.
5. **Cost & Privacy Sovereignty**: Keep-Your-Own-Key (KYOK) proxying for caller-funded LLM inference.

---

## 📊 Comprehensive Matrix

| Category | Project / Platform | Target Scope | NAT Traversal / Egress-Only | Protocol Support | Identity & Trust Model | State / HITL Support | Vendor Lock-in |
|---|---|---|---|---|---|---|---|
| **Agent Relays** | **Agent Souk** | Open Agent Relay & Gateway | ✅ Outbound gRPC Stream | AG-UI + A2A | Ed25519 Keypair + EdDSA Actor Chains | ✅ ParadeDB (Thread DAG + Pause/Resume) | ❌ 100% Self-Hosted & Open |
| | **a2a-relay** | Outbound WebSocket relay | ✅ Outbound WebSockets | A2A only | ⚠️ Shared secret JWT (Single key) | ❌ Stateless forwarder | ❌ Open Source |
| | **MCP Tunnels** (OpenAI / Anthropic) | Local tool exposure for LLMs | ✅ Outbound relay | MCP tools only | 🔒 Vendor session token | ❌ None | ⚠️ Tied to proprietary models |
| **Agent Gateways** | **agentgateway.dev** | Ingress security/observability data plane for agent traffic | ❌ Assumes backend already reachable | MCP + A2A + HTTP | 🔑 JWT/API key/OAuth + Cedar-based fine-grained RBAC | ❌ Stateless data plane | ❌ Open Source (Rust; standalone or Kubernetes) |
| **Enterprise Cloud Runtimes** | **AWS Bedrock AgentCore** | Managed agent hosting + identity/credential service | ❌ Agents run *inside* AWS (opposite model — no outbound relay needed) | Bedrock-native; A2A interop via ecosystem partners | 🔒 AWS IAM + per-agent ARNs, dedicated Identity service | ⚠️ Managed by AWS | 💰 AWS Ecosystem |
| | **Google Cloud Agent Marketplace** | Commercial A2A agent registry & marketplace | ❌ Requires the agent already deployed somewhere reachable (typically GCP) | A2A Protocol | 🔒 GCP IAM / OAuth | ⚠️ Managed GCP State | 💰 Google Cloud |
| **Decentralized Identity** | **AgentConnect / ANP** | Decentralized Agent Identity | ❌ Requires reachable URL | DID Protocol | 🌐 W3C DID Standard | ❌ Protocol spec only | ❌ Open Specification |
| | **NANDA (MIT)** | Federated Registry & Indexing | ❌ Identity / Discovery layer | Academic Spec | 🌐 Federated Registries | ❌ None | ❌ Open Specification |
| **Monetization & Payment** | **x402 / Nevermined** | Payment & Billing Rails | N/A (Payment layer) | HTTP / x402 headers | 💳 Web3 / Credit card rails | ❌ None | ❌ Open Specification |

*(Deliberately excludes pure LLM-routing proxies like LiteLLM/Portkey/Cloudflare AI Gateway — they sit between an agent and its own LLM provider, a different layer than agent-to-agent/agent-to-human reachability, and don't have a meaningful answer to most of these columns. Souk's KYOK feature is adjacent to that layer but isn't a competitor to it — see [`keep-your-own-key.md`](keep-your-own-key.md).)*

---

## 🔍 Detailed Competitive Breakdown

### 1. vs. `a2a-relay` (Architectural Nearest Neighbor)
- **Similarities**: Both use outbound connections from the agent to a relay server to bypass NAT / firewalls without public IPs.
- **Differences**:
  - `a2a-relay` only handles A2A (JSON-RPC) traffic and is stateless. It does not provide human streaming endpoints (**AG-UI**).
  - `a2a-relay` relies on a single shared secret across the entire relay. Any caller with the secret can mint tokens for *any* agent (impersonation vulnerability).
  - **Agent Souk** gives each agent a cryptographic **Ed25519 identity keypair**. An agent *is* `(public key, name)`, so only the key holder can register one or be given its runs.

### 2. vs. `agentgateway.dev` (genuinely closest gateway comparison)
- **Similarities**: Both natively bridge A2A (and, in agentgateway's case, MCP) traffic; both are open-source and self-hostable.
- **Differences**:
  - agentgateway is an *ingress* data plane — a single Rust binary (runnable standalone or on Kubernetes) that assumes the backend agents it fronts are already reachable on the network it's deployed into. It solves governance/policy (JWT/API-key/OAuth auth, Cedar-based fine-grained RBAC down to the tool level) for infrastructure you already control, not NAT traversal.
  - **Agent Souk** solves the opposite problem: making agents reachable *at all* when they can't host an open endpoint (laptop, behind NAT, private network) — governance/RBAC of the kind agentgateway provides is not something souk currently offers.

### 3. vs. Cloud-Managed Agent Runtimes (AWS Bedrock AgentCore, Google Cloud Agent Marketplace)
- **Similarities**: Both provide a real per-agent identity model (AgentCore: AWS IAM + per-agent ARNs via a dedicated Identity service; Google Cloud Marketplace: GCP IAM/OAuth) and durable session handling.
- **Differences**:
  - Both require the agent to already run *inside* that vendor's cloud (AgentCore's managed runtime; GCP-hosted endpoints for Marketplace listings) — reachability isn't a problem they need to solve, since the vendor's own infrastructure already hosts and can reach the agent.
  - **Agent Souk** leaves agent execution 100% self-hosted, anywhere, and solves reachability *and* protocol bridging as the thing it adds on top — at the cost of the enterprise governance tooling a hyperscaler ships out of the box.

---

## ⚠️ A2A Protocol Deviations & Interoperability Notes

Souk implements the A2A protocol but deliberately deviates from the spec in a few places. These deviations affect real ecosystem interoperability: a standard A2A client (e.g. Google ADK, Vertex AI) following the spec strictly may observe unexpected behaviour in the following areas.

### 1. `params.id` (Caller-Chosen Task ID) is Accepted But Ignored

A2A's original `tasks/send` / `tasks/sendSubscribe` accepted a caller-supplied `params.id` as the canonical task identifier. Souk accepts this field to avoid rejecting clients written against that version, but **does not use it as the task/run identifier**. The canonical identifier is always Souk's own database-generated `run_id`.

This deviation has mostly dissolved on the spec's side rather than souk's: the current methods are `SendMessage` / `SendStreamingMessage`, whose request has nowhere to put a caller-chosen task id at all. `Message.taskId` is a reference to an *existing* task, not a request to create one under that name, and souk honours it as such (see `souk/protocols/a2a.py`'s `_context_of_task`).

**Practical impact**: A caller that stores `params.id` and later calls `tasks/get` or `tasks/cancel` using that value will get a 404. It must use the `run_id` Souk returns in the response instead.

**Why**: Souk fronts many agents behind one relay. Letting callers choose their own task IDs across multiple independent providers would require global uniqueness enforcement that the current single-Souk design does not have a clean owner for. Database-generated IDs are the simplest correct answer given the current architecture.

### 2. `contextId` is Souk-Issued, Never Caller-Created

The A2A spec allows callers to supply a `contextId` (previously called `sessionId` in an earlier draft) to group tasks into a conversation. Souk accepts a `contextId` only if it was previously issued by Souk itself (`repo.ensure_thread`). An unknown `contextId` is a 404, not silently created.

**Practical impact**: A caller that mints its own `contextId` before the first call (expecting Souk to initialise a thread under that name) will get a 404. Omitting `contextId` on the first call is the correct pattern — Souk generates and returns one, which the caller then passes on subsequent calls to continue the same thread.

**Why**: Thread IDs serve as capability tokens in Souk's trust model (see [`agent-provider-guide.md`](agent-provider-guide.md)'s section on this). Accepting arbitrary caller-chosen IDs would let any party claim ownership of a thread they did not originate.

### 3. `referenceTaskIds` is Informational Only

Souk records `Message.referenceTaskIds` for lineage tracking (`GET /threads/{id}/tree`) but does not use it to infer session continuity. A sub-agent call that includes `referenceTaskIds` pointing to a parent task does **not** automatically continue the parent's thread — `contextId` continuity must be managed explicitly by the caller.

**Practical impact**: Callers that rely on `referenceTaskIds` to imply session grouping (as some A2A implementations do) will get independent threads per call rather than a continued session.

---

## 💡 Summary

Agent Souk occupies a unique intersection in the 2026 AI infrastructure landscape:
It is **neither just an LLM proxy nor just a cloud hosting platform**. It is an **open, zero-config reachability relay and protocol bridge** that makes agent-to-human (AG-UI) and agent-to-agent (A2A) interaction effortless across network boundaries.
