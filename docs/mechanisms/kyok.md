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

The full design record, including the two prior designs this replaced and
why they failed, is
[`design/keep-your-own-key.md`](https://github.com/hukaichun/AgentSouk/blob/main/design/keep-your-own-key.md).
