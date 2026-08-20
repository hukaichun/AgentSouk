# Agent Souk

souk is a relay for AI agents: providers connect out to it — from a
laptop, behind NAT, inside a private subnet — and souk opens the doors
callers walk through. It carries standard protocols unchanged, verifies
every identity it is shown, and intervenes in nobody's behavior. The
core is a network-free Python library; putting it on a wire is a
downstream job.

This site is organized as six chapters, in reading order:

**[The integration contract](integration-contract.md)** — the
declaration to anyone plugging in. Callers speak AG-UI or A2A with a
standard client, unmodified, and every souk invention on that side is
opt-in. Providers speak standard shapes — AG-UI runs, OpenAI completions
— and souk opens the doors for them, with the mandatory plumbing
published as data.

**[Mechanisms](mechanisms.md)** — the six things souk actually invented:
identity as an Ed25519 keypair, actor chains, runs and cancels as
requests, provider quality counters, keep-your-own-key, and
responsibility chains. Everything else is a standard carried unchanged
or an implementation detail of one of these six.

**[Core components](core-components.md)** — how the library implements
it: what is persisted, how the dispatch trunk moves runs and completions,
how verification works, and where each mechanism lives in the tree.

**[The SDKs](sdks.md)** — the two pure contract packages providers build
with (`souk-provider-sdk`, `souk-llm-provider-sdk`): no transport, no
souk dependency, every byte pinned — including the wire itself, exercised
end to end without a socket.

**[Writing a transport](writing-a-transport.md)** — core hands back
objects and pure functions; this is how you put them on a wire. The
opening handshake in order, with the exact calls, and the parts souk
deliberately leaves to you.

**[Design records](design-records.md)** — why souk is shaped this way,
including the shapes it had first and stopped having. Each entry was
argued from something that happened: a probe that returned the wrong
answer, a bug that reached a caller, a measurement taken before a
rewrite.

The byte-level authority behind all of it is
[`contract-vectors.json`](contract-vectors.json), consumed by souk's own
test suites and replayable by an implementation in any language. The
working design record lives in the repository under
[`design/`](https://github.com/hukaichun/AgentSouk/tree/main/design),
which each design record links into; pages graduate from there to this
site deliberately. Serving — HTTP, WebSockets, deployment — lives
downstream in
[AgentSoukServer](https://github.com/hukaichun/AgentSoukServer).
