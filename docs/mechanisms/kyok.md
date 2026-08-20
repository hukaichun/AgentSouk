# Keep your own key (KYOK)

Part of [souk's mechanisms](../mechanisms.md).

Running an agent costs LLM tokens, and the obvious arrangement — hand
your API key to whoever hosts the agent — leaves the credential on
infrastructure you don't control. KYOK inverts it: the agent's host never
holds the key. The agent still writes ordinary code that calls "an LLM";
it is calling souk, and souk relays each completion to an **LLM
provider** — a first-class provider that registered and attached with the
same identity machinery as any agent provider, holds a real key, and
serves the completion under its own policy.

## Binding

A caller opts a run in with `metadata.kyok`, naming an offering
(`providerKey` + model name) and optionally a `context` — a field souk
relays to the LLM provider untouched and never interprets; what it means
is between the two ends. The binding is checked against the durable
roster at run start (a typo fails the run immediately, not on the first
completion) and names an offering, not a connection — the provider can
drop and re-attach mid-run.

When a bound run delegates, souk itself copies the binding to the child
run — never the delegating agent, which would otherwise be an agent
holding the caller's `context`. One opt-in therefore covers a run tree,
and the LLM provider polices the tree's shape with the material each
completion carries.

## Authorizing a completion

The agent's `api_key` is a run-scoped bearer token — HMAC-signed by souk,
carrying exactly the run id, the agent's identity, and an expiry. Three
checks gate every call: the token is souk's own and unexpired; the run it
names is still live for that agent (a leaked token dies with its run, not
with its TTL); and the call itself is signed, fresh, by the agent
provider's own key over the token, a timestamp, and the request body's
hash.

## Serving, refusing, and what souk relays

Each delivered completion carries the run id, the *proven* calling-agent
identity, which model was addressed, the `context`, and the run's actor
chain — everything a policy needs, with no need to trust souk's summary.
Wire shapes are OpenAI's chat completions, unmodified. A refusal raised
as `CompletionRefused(payload)` reaches the calling agent **as data** —
souk relays the payload intact; the vocabulary inside is the parties'
own. Any other failure is an unstructured error. souk counts what it saw
(served, refused, failed — see
[quality counters](quality.md)) and decides nothing.

## How a refusal reaches the caller

A refusal is the LLM provider's policy working, not an error souk has an
opinion about — so souk relays it as **data** and reads none of it.

The provider raises `CompletionRefused(refusal)` carrying its own dict.
souk picks the payload off by attribute name, duck-typed: the two
packages do not import each other, and the attribute name itself is
pinned in the contract so it cannot drift. Anything that is not a dict
is not treated as a structured refusal.

There are two shapes, depending on where the caller is when it happens:

- **Before the stream starts**, it surfaces as `KyokRejected` carrying
  the provider's refusal and a status. The statuses are a fixed map:
  401 for a bad token or signature, 400 for a malformed body, 403 for an
  inactive run or an unregistered agent, 503 for a missing binding or a
  detached provider, 502 for the provider's own call failing.
- **Mid-stream**, the relay emits one final `{"error": ...}` frame
  carrying the same payload. Note that this path ends the stream
  *without* the trailing `[DONE]` sentinel a successful stream emits — a
  client that waits for `[DONE]` before acting will wait forever.

An exception that is not a structured refusal collapses to plain prose
instead, and the quality counters record it as `failed` rather than
`refused`. Both are counted; neither is judged — see
[quality counters](quality.md).

The full design record, including the two prior designs this replaced and
why they failed, is
[`design/keep-your-own-key.md`](https://github.com/hukaichun/AgentSouk/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/keep-your-own-key.md).

## Design records

Why this is shaped the way it is, and what it was shaped like first:

- [KYOK replaced two designs, both failing for one reason](../design-records.md#kyok-replaced-two-designs-both-failing-for-one-reason)
- [An inter-chunk timeout kills slow models and blames the wrong side](../design-records.md#an-inter-chunk-timeout-kills-slow-models-and-blames-the-wrong-side)
- ["Trustless" binding was rejected as false safety](../design-records.md#trustless-binding-was-rejected-as-false-safety)
