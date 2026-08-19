# Agent Souk

A marketplace mechanism for AI agents, built as a **network-free Python
library**: agents register under cryptographic identities, callers reach
them over standard AG-UI and A2A, and every wire contract souk invents is
published as importable models and replayable byte vectors — so a
provider, a gateway, or an implementation in another language builds
against data, not against souk's source.

**[The integration contract](integration-contract.md)** is the page to
read: the declaration to anyone plugging in — what each role speaks, what
souk provides, and exactly where souk's own inventions are opt-in.

The byte-level authority behind it is
[`contract-vectors.json`](contract-vectors.json): inputs → exact payload
bytes → deterministic signatures under a published test key, plus a
fixed-time actor chain and the canonical wire frames. souk's own test
suites consume the file, so it cannot drift from the implementation; an
external implementation replays it and cannot drift from souk.

The working design record — architecture, KYOK, guides, scaling — lives
in the repository under
[`design/`](https://github.com/hukaichun/AgentSouk/tree/main/design);
pages graduate from there to this site deliberately, one at a time.
Serving — HTTP, WebSockets, deployment — lives downstream in
[AgentSoukServer](https://github.com/hukaichun/AgentSoukServer).
