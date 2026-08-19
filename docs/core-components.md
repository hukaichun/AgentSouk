# Core components

souk's core is one importable object — `Souk` — that an embedder
constructs, starts, and hands connections to; everything below hangs off
it. The core is network-free by test: it knows a database and nothing
else, and which wire anything arrives over is a serving-layer choice made
elsewhere.

## Persistence

The durable half: what souk remembers across restarts, in eight tables on
one dialect-neutral code path (SQLite by default, Postgres by
configuration), with the migration chain shipped inside the package.
→ [Details](core-components/persistence.md)

## The dispatch trunk

The moving half, four lanes: the caller-facing doors (AG-UI and A2A), the
translation that makes them one shape, the agent-provider lane
(offer → claim → pipeline), and the LLM-provider lane (completion relay).
Two live rosters, one shared substrate.
→ [Details](core-components/dispatch.md)

## Contract and identity

Core's side of every signature: verification of the seven payload
families, link-open challenges, souk's own signing identity, the actor
chain verifier, and the envelope models — each with an independent twin
in the SDKs and byte vectors pinning both.
→ [Details](core-components/contract-identity.md)

## Mechanism to code

Where each of [souk's six mechanisms](mechanisms.md) actually lives in
the tree — an index, so the mechanism pages stay about meaning and this
page about location.
→ [Details](core-components/extensions.md)

## Support

The pieces that keep the trunk honest: push-style change notifications so
a serving layer never polls, health sweeps that fail what stalled, and
the settings object where every deliberate switch lives.
→ [Details](core-components/support.md)
