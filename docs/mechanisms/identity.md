# Identity is an Ed25519 keypair

Part of [souk's mechanisms](../mechanisms.md).

A provider's identity to any souk it connects to is its Ed25519 keypair —
not an account souk issues. An agent is `(public key, name)`; an LLM
offering is the same pair; a name is deliberately not an identity and is
not exclusive. A short fingerprint derived from the key supports
human-friendly resolution, trust-on-first-use: two keys colliding on one
fingerprint is an error, not a merge. souk has a keypair of its own
(`SoukIdentity`, configured, never generated silently), so a provider can
pin the souk it means to serve and detect an imposter before producing
anything worth stealing.

## Everything that changes the roster is signed

Seven payload families, each under its own domain tag so a captured
signature for one purpose can never be replayed as another:

| domain tag | signed by | authorizes |
|---|---|---|
| `souk-register` | agent provider | registering agent names |
| `souk-register-llm` | LLM provider | registering model offerings |
| `souk-delete-agent` | agent provider | deleting one agent record |
| `souk-delete-llm` | LLM provider | deleting one offering record |
| `souk-kyok-call` | agent provider | one KYOK completion call |
| `souk-connect-provider` | connecting provider | opening a link |
| `souk-connect-souk` | souk | answering a link-open |

Registration and deletion sign over a timestamp inside a freshness
window. Link-open does not: a self-chosen timestamp is replayable for its
whole window by anyone on the path, so opening a link answers a
**challenge the verifier chose** — souk mints a single-use nonce, the
provider signs it together with its own nonce, the names it intends to
serve, and the souk key it means to connect to — the recipient is in the
signed bytes, so a proof coaxed out by one souk cannot be relayed to
attach at another — and souk's answering signature (over both nonces,
under a distinct role tag so neither proof reflects as the other) is what
the provider verifies against its pinned souk key. In-process connections authenticate
the same way, automatically — sharing a process is not a reason to skip
identity.

## Roster rules

Registration is a signed batch, and re-registering a subset withdraws the
omitted names from live serving. Attaching — declaring which registered
names a connection serves right now — requires prior registration and
never implies it. Deletion is its own signed act and is refused while the
name is in use. souk holds one connection per role: a re-attach under the
same key replaces the old connection; replicas are the provider's own
concern behind its single connection.

## Published as data

Every payload above is exported by the SDKs — the agent families by
`souk-provider-sdk`, the LLM families by `souk-llm-provider-sdk` — as
independent twins of souk's implementation: neither SDK imports souk nor
souk them (the LLM SDK does share the provider SDK's keypair class,
because identity is identity). Each is pinned byte-for-byte in
[`contract-vectors.json`](../contract-vectors.json), with deterministic
signatures under a published test key. CI fails if a payload family goes
unpublished.
