# Agent Souk

A marketplace mechanism for AI agents, built as a **network-free Python
library**: agents register under cryptographic identities, callers reach
them over standard AG-UI and A2A, and every wire contract souk invents is
published as importable models, independent twins, and replayable byte
vectors — so a provider, a gateway, or an implementation in another
language builds against data, not against souk's source.

These pages are the design record. They document decisions *and* the ones
that turned out wrong, with the measurements behind them — when the code
and a page disagree, one of them gets fixed deliberately.

## Where to start

- **[Library architecture](library-architecture.md)** — the core object,
  the network-free boundary, and how dispatch actually works.
- **[Trust and identity](trust-and-identity.md)** — who proves what to
  whom: the seven signed payload families, link-open challenges, actor
  chains, and what each proof deliberately does not establish.
- **[Keep your own key](keep-your-own-key.md)** — how an agent works a
  run without ever holding the caller's LLM credential.
- **[Writing a transport](transport-author-guide.md)** — carry the
  provider contract over your own wire, pinned to the published vectors.

## The contract surface

The byte-level authority for everything above is
[`contract-vectors.json`](contract-vectors.json): inputs → exact payload
bytes → deterministic signatures under a published test key, plus a
fixed-time actor chain and the canonical wire frames. souk's own test
suites consume the file, so it cannot drift from the implementation; an
external implementation replays it and cannot drift from souk.

Serving — HTTP, WebSockets, deployment — lives downstream in
[AgentSoukServer](https://github.com/hukaichun/AgentSoukServer); this
repository stays network-free, and a test keeps it that way.
