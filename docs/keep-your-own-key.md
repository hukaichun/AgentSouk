# Keep Your Own Key (KYOK)

**Status: experimental.** Two changes on this page are ahead of the gateway
repo, which is not this repo's to edit: `KyokAdapter.respond` takes decoded
chunks rather than NDJSON bytes, and the call-time signing payload gained its
operation prefix. Both need `souk_server/ws_kyok.py` and
`souk-agent-sdk/kyok_auth.py` to follow before a real provider's calls are
accepted again — tracked as
[AgentSoukServer#14](https://github.com/hukaichun/AgentSoukServer/issues/14).
The session-hash fix below needs nothing downstream.

All the pieces have real test coverage: souk's side (`souk/kyok.py`,
`souk/protocols/kyok.py`), the caller's bridge and its port
(`souk-caller-sdk/`, here), the provider-side signer (`kyok_auth.py` in
souk-agent-sdk) and the WebSocket carrier (souk-client-sdk; both of those
live in AgentSoukServer, next to the gateway that serves them).

**The loop itself is now exercised end to end** — a provider asking for a
completion, the caller's own code answering, chunks arriving back — in
`souk/tests/test_kyok_in_process.py`, over `souk_caller_sdk.InProcessLink`.
Until that existed, every KYOK test in every repo stopped at a frame: they
drove souk's adapter with hand-made queue entries, or a socket with
hand-made JSON, and nothing joined the two ends.

What is still untested is the same loop over a real network with three real
processes and a real key — `docker compose up`, not a suite. And the scope
this stays "experimental" for regardless: the pending-completion registry is
in-memory and single-process on souk's side, and the caller's bridge holds no
durable state either, so there is no recovery path if any of the three dies
mid-relay. See "Scope / limitations" below.

## Problem

Running an agent costs LLM tokens. The obvious way to pay for that is
"bring your own key" — the caller hands their API key to whoever hosts the
agent. That's a trust problem: the key sits on infrastructure the caller
doesn't control, for the lifetime of however long that host wants to keep
it around.

KYOK inverts this: the caller's key never leaves the caller's own
machine. The agent provider still writes ordinary code that calls "an
LLM" — it just happens to be calling souk, and souk quietly hands the
actual completion off to whoever's paying for it.

## Shape of the solution

souk exposes an OpenAI-compatible `chat/completions` endpoint. A provider
points its model client at it exactly the way `providers/pydantic-ai-agent`
already points `custom-openai:` at any OpenAI-compatible host (see
`pydantic_ai_agent/main.py::resolve_model`) — no new SDK, no new wire
format on the provider side, one `base_url` + one `api_key`.

souk does not hold a real LLM key behind that endpoint. The "api_key" a
provider sends there is actually a **run-scoped bearer token** souk minted
for this one run. souk uses it only to figure out *whose* run this
completion belongs to, then hands the request off to that run's caller,
who relays it to a real LLM using their own key and streams the response
back — souk relays that back to the provider as if it had called the LLM
itself.

The caller's bridge holds one WebSocket to the gateway for the run's
duration (`WS /ws/kyok`; see the gateway repo's docs/server-mode.md for the
frame table). This replaced a `GET /kyok/poll` + `POST /kyok/respond/{id}`
pair, and the replacement was a security fix as much as a transport one:
`respond` authorised an answer on nothing but possession of the
`request_id`, so anyone holding that string could supply the "LLM" output a
provider's agent then acts on. On a socket, an answer is accepted only for a
request delivered on that same connection.

```
provider                    souk                      caller's KYOK bridge
   |                          |                                 |
   |                          |<====== WS /ws/kyok, hello ======>|  (held open)
   |                          |                                 |
   | POST /kyok/v1/chat/completions                              |
   |------------------------->|                                 |
   |                          |---- completionRequest --------->|
   |                          |                       (calls real LLM
   |                          |                        with own key)
   |  SSE chunk               |<--------- chunk ----------------|
   |<-------------------------|                                 |
   |  SSE chunk               |<--------- chunk ----------------|
   |<-------------------------|                                 |
   |  data: [DONE]            |<--------- done -----------------|
   |<-------------------------|                                 |
```

`requestId` multiplexes, so one socket serves concurrent completions.

Two things this is deliberately *not*, ruled out earlier in the design
discussion:

- **Not `pause.py`'s HITL/resume convention.** That machinery pauses an
  AG-UI *run* and resumes it later with a new message on the same
  thread — it's a state-machine concept about runs, and it can't carry a
  live streaming response back into the middle of an in-progress run
  anyway (a paused run ends its stream; this needs the opposite — an
  open channel *during* the run).
- **Not the run-dispatch path.** That's souk
  dispatching work *to providers*. The caller here is not a provider and
  never registers an agent — it's whoever is talking to souk over the
  public AG-UI HTTP surface (`souk-client-sdk`).

KYOK is a third, independent channel, orthogonal to both.

## The caller's side is a port, not a socket

What the caller does — claim a completion, call their own model, stream the
answer back — is stated transport-free in `souk-caller-sdk` (here), the peer
of `souk-provider-sdk`. Two methods (`CallerLink.claim` / `.answer`) and one
callable the caller supplies:

```python
CompletionSource = (body: dict) -> AsyncIterator[chunk: dict]
```

That callable is deliberately not a library. `litellm` is a fine default and
it lives downstream, in the package that owns a socket — because *which model,
which vendor, which key, what it costs and whether to refuse* is the one
decision KYOK exists to leave the caller, and a package that made it for them
would be undoing the point. It is also the natural home for a per-run spend
ceiling ([#26](https://github.com/hukaichun/AgentSouk/issues/26)): only the
caller's side can price anything.

`InProcessLink` is a carrier like any other, and gets no shortcut a remote
bridge does not — same session routing, and the same handshake if the bridge
side ever gains a credential. Its purpose is not deployment; nobody runs a
caller inside souk. It is that the loop becomes testable without a gateway,
three processes and a real key, which is how
`souk/tests/test_kyok_in_process.py` exists at all.

## Binding a run to a caller's bridge session

The provider's LLM client is normally built once per container/agent
config, long before any particular run exists — so the token it uses
can't be static (multiple concurrent runs, from different callers, would
collide on the same credential). Instead:

1. Before starting a run, the caller mints its own `session_id` locally
   (`KyokBridge.open()` — just `secrets.token_hex(16)`; souk never hands
   one out, it accepts whichever session_id shows up) and opens
   `WS /ws/kyok`, presenting it in the `hello` frame.
2. The caller starts the run as usual
   (`POST /agui/{name}` via `souk-client-sdk`), passing
   `metadata: {"kyok": {"sessionId": "<session_id>"}}`.
3. `protocols/agui.py::build_forwarded_props`, seeing `metadata.kyok.
   sessionId`, mints a token via `souk.kyok.issue_kyok_token(run_id,
   session_id, agent)` — souk already knows which agent this is,
   the run's own — and places it in the AG-UI `forwardedProps` delivered
   to the provider: `forwardedProps.kyok = {"token": "..."}`.
   `forwardedProps` is the existing AG-UI passthrough field for exactly
   this kind of app-specific context (see `souk/agui.py`) — no schema
   change needed. Deliberately no `baseUrl` here: `settings.
   public_http_url` is for external callers and is frequently
   unreachable from inside a provider's own container/network (see
   docker-compose.yml — providers reach souk at `http://souk:8000`, not
   `http://localhost:8000`) — the provider already knows its own
   `souk_http_url` and builds `{souk_http_url}/kyok/v1` itself.
4. The provider reads `forwardedProps.kyok.token` off its `RunAgentInput`
   and builds its model client against `{souk_http_url}/kyok/v1` with
   that token as its `api_key`, for this run only, instead of whatever
   static LLM config it would otherwise use. See `providers/
   pydantic-ai-agent`'s `resolve_kyok_model` for the ~15-line version of
   this.
5. A completion request arriving at `/kyok/v1/chat/completions` decodes
   the bearer token back into `(run_id, session_key, agent)`,
   cross-checks `agent` against `souk.broker.get(run_id)`'s live agent
   (rejecting if the run isn't currently live, or somehow belongs to a
   different agent than the token claims — see the next section for why
   this matters even though the second half of that check can never
   actually fire today), then queues the request under `session_key` and
   blocks relaying its response back once the bridge's chunks arrive.

**`session_key`, not the session id.** The token is signed, not sealed:
whatever is in it is readable by the provider, and the provider is the one
party KYOK exists to keep the caller's key away from. The token used to
carry the session id itself, so a provider could decode its own token, open
`/ws/kyok` under the caller's session, and be handed a completion belonging
to a *different* provider on it — that provider's prompt to read, its answer
to write, which is injected tool input for whatever agent acts on the
answer. It was probed against a live app, not reasoned about. What travels
now is `souk.kyok.session_routing_key` — a SHA-256 of the id — and the two
sides meet at the hash: a bridge presents the preimage, a provider learns a
value that opens nothing. Encrypting the token to the provider's key would
not have helped; the attacker is the party it is encrypted *for*.

The token is signed the same way `souk.identity` once signed provider
session tokens (HMAC over a base64 JSON body, same
`settings.token_signing_secret`) — that call is gone, so this is now the
only thing that secret signs. See `souk/kyok.py`.

## Binding a token to the specific run and provider that hold it

A provider's identity is real — its Ed25519 keypair
(`souk_provider_sdk.identity`), proven at registration and re-proven when it
connects (`souk/identity.py`).
But a bearer token by itself only proves souk minted it, not who's
presenting it — a leaked or merely-retained token is just as usable by
whoever holds the string, regardless of whether they can make sense of
its contents (encrypting it so only the right provider could *read* it
wouldn't help either — replaying an opaque blob as a bearer value
doesn't require understanding it). Two separate checks close this, at
two different layers:

**1. The token is bound to a specific run, checked against souk's own
live state.** souk already knows, at the moment it mints a KYOK token,
exactly which agent the run belongs to — so the token says so
explicitly (`issue_kyok_token(run_id, session_id, agent)`) instead of
leaving that implicit, and `chat_completions` re-derives the *current*
truth from `souk.broker` and requires them to match (`KyokAdapter.
complete`). This closes a real
gap: without it, a KYOK token remains a usable bearer credential for its
full `KYOK_TOKEN_TTL_SECONDS` (an hour) even after the run it was minted
for has finished and its provider process is long gone. Checking against
`broker.get(run_id)` (which forgets a run the instant its pipeline
terminates — see `broker.py`'s `_pipeline`) collapses that window to
"exactly as long as the run is genuinely still in flight." The agent
*mismatch* half of this check can't actually fire yet — a run's agent is
fixed forever from the moment `repo.create_run` assigns it — it's there
anyway as an explicit, checked invariant rather
than one only true by construction, so it starts doing real work the
moment that assumption ever stops holding (some future run hand-off/
reassignment path), instead of silently going stale.

**2. Every call is signed, live, by the calling provider's own identity
key.** This is the part (1) can't cover — (1) only checks *which run*
the token claims to be for, never *who's actually presenting it right
now*. `souk_agent_sdk.KyokSigningAuth` is an `httpx.Auth` that signs each
outgoing completion request with this provider process's own Ed25519 key
over `souk_provider_sdk.identity.kyok_call_payload` —
`souk-kyok-call:{bearer}:{timestamp}:{sha256(body)}` — attached as
`X-Souk-Kyok-Timestamp`/`X-Souk-Kyok-Signature`. The operation prefix is
the same device registration and deletion use (see `souk/identity.py` for
the collision that put it there); KYOK was the one signed payload without
one, and escaped collision only because it would have needed a bearer equal
to the literal string `souk-register`. `KyokAdapter._verify_caller` checks
the signature against the public key the token names — souk signed the
token itself, so that key is souk's own source of truth — over this exact
request (the body hash stops a captured signature being
replayed against a *different* request; the timestamp, checked against
the same freshness window as every other signed request in this system,
`souk.identity.SIGNATURE_FRESHNESS_WINDOW_SECONDS`, stops one being
replayed at all after ~60s).

This is the one place KYOK asks a provider for something beyond
"point an unmodified OpenAI client at a URL+key": `resolve_kyok_model`
(`providers/pydantic-ai-agent`) builds its `httpx.AsyncClient` with
`auth=KyokSigningAuth(signing_key)` — reusing the same identity key the
process already generated for registration, not a new keypair — and
hands that to `OpenAIProvider(http_client=...)`. Everything else about
the request (URL, body, api_key) stays exactly OpenAI-shaped; this just
adds two headers underneath.

### Opening a bridge session: what is and is not checked

`session_id` (`KyokBridge.open()`) is a locally-generated random value —
souk never issues or vets one, and `/ws/kyok` serves whoever presents one.
A session is not an identity or a capability grant; it is a rendezvous
label whoever starts a run picks, and `metadata.kyok.sessionId` is only
ever set by whoever calls `POST /agui/{name}` to *create* that run.
Metadata is fixed at `repo.create_run`/`reopen_run` time and not mutable
afterwards, so nobody can attach their own session to a run they did not
start.

Which leaves one thing holding the bridge side up: **knowing the session id
is the whole proof.** 128 bits of entropy, minted locally, and — now — never
handed to anyone but souk.

That last clause is new, and this section used to assert it as though it
had always been true. It had not: the KYOK token carried the session id in
plaintext to the provider (see "Binding a run to a caller's bridge session"
above). The hash closes the disclosure souk was itself creating. It does not
turn the id into something better than a bearer secret:

- anything that *does* learn one — a log, a TLS-terminating corporate proxy —
  can hold that session and be served completions on it;
- souk cannot tell two sockets on one session apart, so a squatter coexists
  with the real bridge and they race for each request.

Closing those needs the bridge to prove something rather than present it:
a keypair the caller mints per run, named in `metadata.kyok`, proven by
challenge-response on `hello` — the same shape the provider handshake
already uses — with delivery gated on the socket having proven the key that
run names. That is a design change, not a patch, and it is not done. It
needs no notion of a user account: the keypair is as anonymous and as
per-run as the session id, and differs only in never being transmitted.

## Scope / limitations (known, not oversights)

- **Single souk process only.** `KyokBridge`'s registry (`session_id ->`
  pending completions) is in-memory, like `broker.RunBroker` — it does
  not survive a restart and does not work if souk is ever split across
  multiple processes (a request queued against one process's registry
  is invisible to another). Consistent with the rest of souk's current
  single-process assumption; revisit together if that ever changes.
- **One bridge session serves one run at a time in this first cut.** A
  caller's `session_id` is consumed by exactly the run it was declared
  for — reusing one long-lived bridge across many sequential runs (e.g.
  every turn of one long thread) is a natural follow-up, not implemented
  here.
- **The caller's bridge is trusted to actually call an LLM.** souk
  doesn't validate what comes back beyond it being well-formed
  OpenAI-shaped chunks — same trust model as "the provider says whatever
  it wants over AG-UI"; souk is a relay, not a validator of LLM output.
- **`CLAIM_TIMEOUT_SECONDS` (30s) does not mean what it is named.** It is
  applied to *every* `queue.get()` in `KyokAdapter._drain`, so it is an
  inter-chunk idle timeout, not a claim timeout. A bridge that claims
  instantly and whose model takes longer than 30s to produce its first
  token gets the completion killed — and the provider is told "no KYOK
  bridge claimed this completion in time", which is false and points at
  the wrong side. Probed with the constant patched down; not yet fixed.
  It wants splitting into two: one bound on the claim, one on the gap
  between chunks.
- **No cap on completions in flight.** `KyokBridge.submit` takes whatever a
  live run's provider sends, each holding a `PendingCompletion` and a queue
  until it times out. That is souk's own memory, so it is not the same
  question as a *spend* ceiling (AgentSouk#26, whose answer is in the
  caller's bridge, since only that side can price anything).
- **An in-flight completion outlives its run.** The run is checked when the
  completion is submitted, not while it streams; forgetting the run
  mid-relay leaves the relay running and the caller paying. Probably right —
  a request already in flight should finish — but it was never a decision.
