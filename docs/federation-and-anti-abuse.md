# Design Notes: Cross-Souk Discovery & Anti-Abuse

> *These are architectural design proposals, not commitments. They exist to capture thinking before implementation begins, so that assumptions can be challenged early rather than baked silently into code.*

---

## 🌐 Part 1: Cross-Souk Discovery

### The Core Insight

Cross-souk discovery is fundamentally a **client-side routing problem**, not a server-side proxying problem.

A naive first design might have Souk A proxy calls to Souk B on behalf of a caller — but that is strictly worse than client-side resolution: it adds latency, makes Souk A a new single point of failure for those calls, and lets Souk A observe traffic that was never meant for it. There is no reason to build inter-souk server-to-server communication if the client can simply be told where to go.

### Proposed Design: `@souk` Addressing + Client-Side Resolution

An agent's full address is `name@souk.example.com`. This is only a **pointer** — it tells the client which Souk to contact. The client then calls that Souk's HTTP surface directly:

```
Caller knows: translator@souk.alice.org
         │
         ▼  (resolve: contact souk.alice.org directly)
POST https://souk.alice.org/a2a/translator/rpc

No inter-souk proxying. No new server-to-server protocol.
```

The call is made directly from the caller to the target Souk. The existing Ed25519 Actor Chain is extended by the caller before the request — provenance integrity is end-to-end, no hop through an intermediary Souk.

### What a Souk Exposes for Discovery

Each public Souk instance publishes a machine-readable federation document:

```
GET https://souk.example.com/.well-known/souk-federation.json
```

```json
{
  "souk_version": "1",
  "http_url": "https://souk.example.com",
  "grpc_url": "souk.example.com:50051",
  "public_key": "<souk's own Ed25519 public key hex>",
  "agents_url": "https://souk.example.com/agents"
}
```

This document lets a client that knows `@souk.example.com` verify it is talking to the right Souk (via the public key), find its endpoints, and discover the current agent roster. No more than that.

### What This Does NOT Solve

Global "find any agent by capability" discovery is a separate, harder problem. The design above does not address it. A caller still needs to know *which Souk* to ask. Cross-souk indexing or search (a way to answer "which Souk has a translation agent?") is an open problem — and probably belongs in an opt-in application layer built on top of the federation document, not in the protocol itself.

---

## 🛡️ Part 2: Anti-Abuse

### The Fundamental Tension

Agent Souk uses a zero-signup Ed25519 identity model: generating a new keypair is effectively free. This means any rate limiting or quota enforced at the `public_key` layer can be defeated trivially — a determined attacker simply generates a new key per attempt.

Any anti-abuse design that ignores this is a speed bump, not a wall.

### The Correct Trust Anchor: The Souk Operator

The most honest answer to "how do you prevent abuse in an open marketplace" is: **you don't — you let the market operator decide what kind of market they want to run.**

This is not a cop-out. It is the same answer the souk metaphor implies. The market itself does not police vendors; the person who owns the space decides who is allowed in. Different operators making different choices is what produces a diverse ecosystem rather than a monoculture enforced by one central rule.

Operators care about who can register along a spectrum — anyone at all (souk's own, only built-in behavior; fine for developer sandboxes and small trusted deployments), a new keypair needing co-signature from an already-registered one (team/org-internal Souk with controlled growth), or an explicit allowlist of permitted public keys (enterprise deployment, closed registry).

Souk itself deliberately implements none of these — the mechanism/policy split holds here too. Since the library split, an operator has two clean seams and needs no souk-side policy hook at either: wrap the gateway's plain ASGI app in their own middleware (the reference gateway is the AgentSoukServer repository, whose `create_app` binds nothing and mounts anywhere), or gate the endpoint from outside with a reverse proxy.

The registration flow already puts the right seam in the right place for this to be someone else's problem, not souk's: every path to becoming a provider goes through one HTTP call, `POST /agents/register` (served by the AgentSoukServer gateway) — the relay channel a worker claims over only becomes reachable *after* that call succeeds and returns a session token, so gating registration is sufficient; nothing about the relay itself needs to be intercepted. An operator who wants a non-default policy puts their own reverse proxy or middleware in front of that one endpoint — reading the request (the claimed `public_key` and `agent_names`, same as souk itself does) and deciding whether to let it through, using whatever logic and however they persist it. This is ordinary HTTP-layer work with no souk-specific protocol to learn, so a coding agent can implement it directly from a plain description of what to allow.

### Secondary Measures (Complement, Not Replace, Operator Policy)

These are useful on top of operator policy, particularly for a Souk that keeps registration open to anyone, but are not substitutes for it:

**1. Proof-of-Work Registration Gate**

Require a short computational challenge before `/agents/register` is accepted. A `sha256`-based challenge (e.g. hash must start with N leading zeros) makes mass registration expensive in CPU time without requiring any identity infrastructure.

Limitation: cloud compute makes PoW cheap at scale. It is a friction increase for casual bot operators, not a hard wall against a motivated attacker. It is most useful combined with per-key quotas.

**2. Per-key Agent Quota**

Limit the number of active agent names any single public key may hold simultaneously (e.g. 10 by default, operator-configurable). Combined with PoW, registering 10,000 fake agents requires generating 1,000 distinct keypairs and solving 1,000 PoW challenges — expensive enough to deter most spam.

**3. Directory vs. Relay Separation**

Even in `open` mode, a Souk can separate *being reachable* from *appearing in the public roster*:
- Any registered agent is reachable via its permanent route `/a2a/id/{agent_id}/...`.
- Appearing in `GET /agents` and in the `souk-directory` UI requires meeting a higher bar (operator-defined: PoW, allowlist membership, or payment).

This lets open Souks remain permissionless for programmatic use while keeping the human-facing directory signal-to-noise high.

**4. Name Display Disambiguation (UI layer only)**

When multiple registered keys share the same display name, `souk-directory` appends a shortened key tag (`translator#a1b2`). This does not prevent name collisions — names are not exclusive by design — but it makes impersonation visible to human users. Programmatic callers should always use `/a2a/id/{agent_id}/...` or filter by public key, never route by name alone.

---

## Summary

The elegant answers are simpler than the complex ones:

- **Discovery**: Client-side resolution via `@souk` addressing. No inter-souk server-to-server routing. Souk exposes a `/.well-known/souk-federation.json` as a machine-readable pointer.
- **Anti-abuse**: Operator policy is the correct trust anchor, and souk deliberately doesn't implement it — an operator who wants one puts their own reverse proxy in front of `POST /agents/register`, the single HTTP call every path to becoming a provider goes through. Cryptographic PoW + per-key quotas are secondary hardening for open-mode Souks. Directory separation decouples "reachable" from "publicly listed."

Neither problem needs a global reputation system or a centralized authority. Both solutions follow directly from the souk metaphor: the market operator sets the rules for their own market; trust between participants is earned, not certified.
