# Keep Your Own Key (KYOK)

**Status: redesigned 2026-08-17; experimental.** This page describes the
LLM-provider design now in the tree (`souk/kyok.py`,
`souk/protocols/kyok.py`, `souk-llm-provider-sdk/`, exercised end to end
by `souk/tests/test_llm_provider_drives_kyok.py`). The gateway repo
(AgentSoukServer) predates this design entirely — its `/ws/kyok` channel,
`souk-client-sdk` bridge and `ws_kyok.py` all speak the abandoned
session-rendezvous protocol and need rebuilding on the new ports before a
remote LLM provider works. What is genuinely proven today is the
in-process loop, on both database backends.

## Problem

Running an agent costs LLM tokens. The obvious way to pay for that is
"bring your own key" — the caller hands their API key to whoever hosts
the agent. That's a trust problem: the key sits on infrastructure the
caller doesn't control, for however long that host keeps it around.

KYOK inverts this: the key never reaches the agent's host. The agent
provider still writes ordinary code that calls "an LLM" — it just happens
to be calling souk, and souk hands the actual completion to whoever is
paying for it.

## The design: KYOK is an LLM provider

Earlier versions of this page described the paying side as an anonymous
"bridge" that rendezvoused with souk over a caller-minted session id.
That design failed repeatedly and instructively — see "History" below —
and the failures shared one root: the bridge was the only actor in the
system with no identity, which meant every question about it ("is this
connection the same party?", "who may open this session?") had no answer.

The reframe: **the party answering completions is an LLM provider, a
first-class provider kind.** It may know who the user is and which agent
provider is calling, and therefore decides its own anti-abuse policy.
"User" and "LLM provider" are deliberately decoupled: a user running a
personal LLM provider with their own key is one deployment; an org
running a shared LLM gateway that recognises its users is another. souk
treats both identically and holds no user identity itself.

Concretely, an LLM provider:

- **registers** with the same Ed25519 machinery as an agent provider
  (`Souk.register_llm_providers`, payload prefix `souk-register-llm`),
  declaring a batch of **model offerings**. An offering is
  `(provider_key, name)` exactly as an agent is — names are deliberately
  not exclusive across identities, because two providers both offering
  `gpt4` is normal, not a race for a word; nobody owns a bare name and
  there is no TOFU to squat.
- **attaches** a connection satisfying `souk.kyok.ConnectedLLMProvider`
  (`public_key`, `complete(request) -> AsyncIterator[ChatCompletionChunk]`)
  declaring which of its registered models it is serving right now —
  `attach_llm_provider(link, model_names)`, the mirror of
  `attach_provider` rule for rule.
- is **resolved per completion call**, so dropping and re-attaching mid-run
  just works: the run's binding names an offering, not a connection.
- **owns policy.** Every delivered completion carries the run id, the
  *proven* calling-agent identity, which of its own models was addressed,
  the caller's `context`, and the delegation chain
  (`souk.kyok.CompletionRequest`); serving, throttling, billing, or
  refusing (raise — souk relays it as a 502) is the LLM provider's
  business. souk never decides on a provider's behalf, and that invariant
  cuts both ways. A spend ceiling (AgentSouk#26) belongs here too.

```
caller                       souk                        LLM provider (key K)
  |                            |  register models + attach (Ed25519) ◀--|
  | POST /agui/{name}          |                                        |
  |  metadata.kyok = {         |                                        |
  |   llmProvider: {providerKey: K, name: "gpt4"},                      |
  |   context: <credential for K, opaque to souk>}                      |
  |--------------------------->| bind run → (K, gpt4) + context         |
  |                            | (context stripped before any persist)  |
  |                            | mint token → forwardedProps            |
  |                            |------ run ------▶ agent provider       |
  |                            |                        |               |
  |                            | POST /kyok/v1/chat/completions         |
  |                            |◀-- (token as api_key, signed) ---------|agent
  |                            |-- CompletionRequest ------------------▶|
  |                            |  (run_id, agent identity, llm_name,    | policy check,
  |                            |   context, actor_chain, body)          | real LLM call
  |                            |◀-------- chunks -----------------------|
  |                            |-- SSE chunks --▶ agent provider        |
```

## Binding a run to an LLM offering

1. The caller — a completely standard AG-UI client, no SDK, no extra
   connection — starts a run with
   `metadata: {"kyok": {"llmProvider": {"providerKey": ..., "name": ...},
   "context": ...}}`. `context` is whatever credential the caller and
   that LLM provider share — an API key, a voucher, an account hint —
   opaque to souk, and the thing that lets a provider serving many users
   tell whose budget a run spends (or serve anonymously; its call).
2. `protocols/agui.py` checks the pair against the durable roster (a typo
   fails the run at start as `InvalidRunInput`, not as a 503 on the
   provider's first LLM call), mints a token into
   `forwardedProps.kyok.token`, and records the binding — offering,
   context, and the run's verified actor chain — in
   `souk.kyok.KyokRelay`. Whether the offering is *attached* is
   deliberately not checked here — reachability is a per-call fact, same
   as agent liveness is for runs.
3. **The context never touches the database.** Run metadata and the run's
   persisted input both come back verbatim through the deliberately
   unauthenticated thread endpoints, and the agent provider holds a
   thread_id — a persisted context would hand the one party KYOK defends
   against the caller's credential, the session-id disclosure with a new
   face. It is stripped from *both* roads at run start (`_split_kyok`;
   the A2A path strips defensively too) and lives only in the in-memory
   binding. `test_llm_provider_drives_kyok.py`'s leak probe asserts the
   whole persisted picture is free of it — and that probe caught the
   second road (the run's input dump) that reading the code had missed.
4. The binding dies with the run: `KyokRelay.discard` hangs off
   `RunBroker`'s forget-listener funnel, the one path every run ending
   crosses. (The registry an earlier design used had no enforced lifetime,
   and 100k entries nothing would reclaim retained 81 MiB — measured.)
5. Metadata is fixed at `create_run`/`reopen_run` and immutable after, so
   nobody can rebind a run they did not start.

## Delegation: the binding follows the run tree

When a KYOK-bound run delegates (an A2A call referencing its own task id,
the standard `referenceTaskIds` lineage), souk itself copies the binding —
same offering, same context — to the child run and mints the child its
own token. Copied by souk and never by the delegating agent, because an
agent forwarding `metadata.kyok` would be an agent holding the caller's
credential. "One-time context" therefore authorizes one run *tree*.

Policing the tree's shape is the LLM provider's job, and it has the
material: the child's binding carries the hop-signed actor chain that
reached that run, each hop verifiable against a registered provider key —
no trust in souk's summary needed. A user running their own LLM provider
(the personal-key deployment this design started from) checks the chain
against the call graph it expects, and the first-hop subject weakness of
actor chains closes itself here: the only party able to verify a subject
claim is the subject, and the subject is the verifier.

Deliberately *not* done: baking run/thread ids into the context for a
"trustless" binding. Completion↔run attribution and run parenthood are
souk-only records — nothing else signs them — so an id in the context
still requires trusting souk for every link. souk-as-relay trust is
irreducible in this architecture, and the ids are unlearnable at
context-minting time anyway.

## Authorizing a completion call

Unchanged from the previous design, because this part worked. The agent
provider's `api_key` is a run-scoped bearer token — HMAC under
`settings.token_signing_secret` (the only thing that secret signs),
carrying exactly `{runId, providerKey, agentName, exp}` and nothing else
(asserted over the whole body by `test_kyok.py`, so nothing caller-side
can quietly reappear in a token the agent provider reads). Three checks
in `KyokAdapter.complete`, all core, none per-transport:

1. the token is souk's own and unexpired;
2. the run it names is still genuinely in flight for that agent
   (`broker.get(run_id)`) — a leaked token dies with its run, not with
   its hour-long TTL;
3. the call is signed, live, by the agent provider's own Ed25519 key over
   `souk-kyok-call:{bearer}:{timestamp}:{sha256(body)}`
   (`X-Souk-Kyok-Timestamp` / `X-Souk-Kyok-Signature`), against the same
   freshness window as every other signed call.

Then: `run → name → attached link`, and the completion is delivered. Not
attached → 503, the same fast-fail shape as an offline agent.

Wire shapes are OpenAI's and come from the `openai` package — types only,
no client — under the same no-hand-written-protocol rule as
`ag-ui-protocol` and `a2a-sdk`. An LLM provider always produces
streaming-shaped chunks; `collapse_stream` folds them for a non-streaming
caller, once, on souk's side. Failures raise: a non-streaming caller gets
an honest 502, a mid-stream failure goes in-band as a final
`{"error": ...}` payload and the stream ends without `[DONE]`.

## History: two designs this replaced, and why

Recorded because the failures were measured, not theorised:

- **Session rendezvous** (the original): the caller minted a session id,
  opened `/ws/kyok`, and the token carried the id to the agent provider —
  which decoded its own token, connected as the bridge, and was handed
  another provider's completion to answer (probed live; injected tool
  input for whatever acted on it). Hashing the id closed the disclosure
  souk itself was creating, but the id remained the entire proof, souk
  could not tell two sockets on one session apart, and the caller-chosen
  key space was unbounded and leak-prone.
- **Single connection** (briefly settled, never shipped): run and bridge
  on one duplex connection, correlation by construction. Died on its own
  correctness: the caller cannot learn souk-minted ids (`run_id`,
  `thread_id`) early enough to present them, and any reconnect path
  reintroduces "who owns this connection" — the exact question the design
  existed to erase.

Both failed for the same reason stated at the top: an actor with no
identity. `souk/tests/test_kyok.py` keeps the regression tests.

## Scope / limitations (known, not oversights)

- **Single souk process.** `KyokRelay` is in-memory, like `RunBroker` —
  same assumption, revisit together.
- **The gateway lags by design generations** — see Status above.
- **souk trusts the LLM provider's answer** to be well-formed chunks and
  nothing more — same trust model as "the agent provider says whatever it
  wants over AG-UI"; souk is a relay, not a validator.
- **An in-flight completion outlives its run.** The run is checked at
  submission, not while chunks stream; forgetting the run mid-relay
  leaves the relay running. Probably right — in-flight work should finish
  — but still never a decision anyone made.
- **No timeout on a hung LLM provider.** Deliberate (the old 30s
  inter-chunk timeout killed slow models while blaming the wrong side):
  a hung stream belongs to whoever is waiting — the agent provider's own
  HTTP timeout, or the serving layer cancelling the relay on disconnect.
