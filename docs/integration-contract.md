# The integration contract: what each party speaks

souk is a relay. It carries runs to agents, completions to LLM providers,
and answers back to callers — and it does not intervene in what any of
them says: it never decides on a provider's behalf, never interprets a
payload that belongs to two other parties, and never records an outcome
it has not observed. Every other promise on this page is a consequence of
that one.

The contract splits by role, because the promises differ. Callers get
standards, untouched. Providers speak standard *shapes*, and souk opens
the doors for them — with plumbing that is souk's own, mandatory, and
published so nobody has to read souk's source to implement it.

| role | you speak | souk provides | souk-invented parts |
|---|---|---|---|
| caller (human UI) | AG-UI, any standard client | the AG-UI endpoint, threads, history | all **opt-in** |
| caller (agent-to-agent) | A2A, any standard client | the A2A endpoint, task lineage | all **opt-in** |
| agent provider | AG-UI shapes: run input in, event stream out | AG-UI **and** A2A facades, both opened by souk | mandatory, **published as data** |
| LLM provider | OpenAI chat-completion shapes: requests in, chunks out | the OpenAI-compatible endpoint agents call | mandatory, **published as data** |

## Callers: standards, and nothing else required

The user–agent seam is AG-UI; the agent–agent seam is A2A. A standard
client of either — unmodified, no SDK of souk's, no extra field — works.
That is a hard rule with a test behind it, and it has teeth in both
directions: when souk seems to need a new field or endpoint, the first
question is whether the protocol already has one, and the answer has
changed designs before.

Everything souk adds beyond the two standards is **opt-in**: binding a
run to a KYOK offering, attaching an actor chain, any metadata mechanism
souk invents. Opting out costs nothing — the run behaves as plain
AG-UI/A2A — and opting in is always the caller's explicit act, never an
inference souk makes.

## Agent providers: speak AG-UI shapes, souk opens the doors

An agent provider hosts nothing and opens no port. It connects **out** to
souk — from a laptop, behind NAT, inside a private subnet — and its whole
protocol obligation is a shape: accept a run input that is an AG-UI
`RunAgentInput`, produce AG-UI events back. Both of souk's caller-facing
doors are opened by souk on the provider's behalf: the AG-UI endpoint,
and the A2A endpoint — an agent becomes A2A-callable without its author
writing a line of A2A, because souk translates every A2A call into the
same AG-UI-shaped run input before dispatch. One shape in, two protocols
served.

Around that shape sits plumbing no external standard defines, so souk
defines it: an Ed25519 identity, signed registration, a challenge-answer
proof when a link opens, a three-valued answer to an offered run. These
are **not opt-in** — they are how a provider exists at all — and the
promise that replaces opt-in is threefold:

1. **minimal** — souk invents only where no standard exists, and the
   invented surface stays as small as the job allows;
2. **published as data** — every payload, model and byte a provider must
   produce or validate is exported by the provider SDK and pinned in
   [`contract-vectors.json`](contract-vectors.json), replayable in any
   language; an implementation never needs souk's source;
3. **guarded** — tests fail souk's own CI when an invented surface goes
   unpublished, so the contract cannot silently grow a private corner.

## LLM providers: serve OpenAI shapes, souk exposes the endpoint

The same pattern, one seam over. An LLM provider (the party holding a
real key — see [Keep your own key](https://github.com/hukaichun/AgentSouk/blob/main/design/keep-your-own-key.md)) also connects
out and also promises only a shape: receive a completion request, stream
back OpenAI chat-completion chunks. The OpenAI-compatible endpoint that
agents call is souk's to expose; the provider behind it is resolved per
call. Policy — serving, refusing, pricing, whose budget a run spends —
is entirely the provider's; souk relays a structured refusal as data and
never reads it.

The plumbing is the same machinery agent providers use (identity is
identity; one keypair may be both), under the same threefold promise.

## Where the inventions live

What the plumbing actually is — the seven signed payload families, the
link-open challenge, actor chains and what they do and do not prove — is
[Trust and identity](https://github.com/hukaichun/AgentSouk/blob/main/design/trust-and-identity.md). How to carry all of it over
a wire of your own is [Writing a transport](https://github.com/hukaichun/AgentSouk/blob/main/design/transport-author-guide.md).
This page is the contract; those are the mechanisms it obliges souk to
publish.
