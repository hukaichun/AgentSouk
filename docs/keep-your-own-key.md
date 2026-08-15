# Keep Your Own Key (KYOK)

**Status: experimental.** All three pieces — souk's side (`souk/kyok.py`
and `souk/protocols/kyok.py`, see `souk/tests/test_kyok.py`), the
provider-side signer (`kyok_auth.py` in souk-agent-sdk) and the caller's
bridge (`kyok_bridge.py` in souk-client-sdk; both SDKs live in the
AgentSoukServer repo now, next to the gateway that serves them) — have
real test coverage. `souk-client-sdk` still has no README (parked, not yet
written). What's still genuinely untested is real network behavior end
to end (these are all in-process/mocked tests, not a live three-process
run) and the scope this stays "experimental" for regardless: the
pending-completion registry is in-memory, single-process on souk's
side, and the caller's bridge holds no durable state either — there is
no recovery path if souk, the provider, or the caller's bridge dies
mid-relay. See "Scope / limitations" below — those gaps are unchanged
by adding tests, and not what this round of work addressed.

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

The caller side of this (`souk.api_llm_bridge`) deliberately mirrors
`souk_agent_sdk.client`'s idle/active shape for providers — long-poll while
idle, open a connection only once there's real work (see that module's
docstring) — rather than asking the caller to hold one connection open for
a run's entire lifetime:

```
provider                    souk                      caller's KYOK bridge
   |                          |                                 |
   |                          |<--- GET /kyok/poll (long-poll) --|  (idle; repeats)
   |                          |                                 |
   | POST /kyok/v1/chat/completions                              |
   |------------------------->|                                 |
   |                          |---- (poll above returns) ------->|
   |                          |                       (calls real LLM
   |                          |                        with own key)
   |                          |<--- POST /kyok/respond/{id} -----|  (streamed body,
   |  SSE chunk               |         one JSON line per chunk   one connection,
   |<-------------------------|                                 |  closes on EOF)
   |  SSE chunk               |<--------------------------------|
   |<-------------------------|                                 |
   |  data: [DONE]            |<--------------------------------|
   |<-------------------------|                                 |
```

`/kyok/poll` is the only thing a caller's bridge does while there's no
completion to serve, and it's a bounded long-poll (`CLAIM_TIMEOUT_SECONDS`-
scale wait, empty response on timeout, immediately re-polled) — exactly
`PollForWork`'s shape, not a held-open connection. `/kyok/respond` is the
one connection either side ever holds open, and only for as long as that
one completion is actually being relayed.

Two things this is deliberately *not*, ruled out earlier in the design
discussion:

- **Not `pause.py`'s HITL/resume convention.** That machinery pauses an
  AG-UI *run* and resumes it later with a new message on the same
  thread — it's a state-machine concept about runs, and it can't carry a
  live streaming response back into the middle of an in-progress run
  anyway (a paused run ends its stream; this needs the opposite — an
  open channel *during* the run).
- **Not the gRPC `AgentSession`/`PollForWork` gateway.** That's souk
  dispatching work *to providers*. The caller here is not a provider and
  never registers an agent — it's whoever is talking to souk over the
  public AG-UI HTTP surface (`souk-client-sdk`).

KYOK is a third, independent channel, orthogonal to both.

## Binding a run to a caller's bridge session

The provider's LLM client is normally built once per container/agent
config, long before any particular run exists — so the token it uses
can't be static (multiple concurrent runs, from different callers, would
collide on the same credential). Instead:

1. Before starting a run, the caller mints its own `session_id` locally
   (`KyokBridge.open()` — just `secrets.token_hex(16)`; souk never hands
   one out, it accepts whichever session_id shows up) and starts
   long-polling `GET /kyok/poll?sessionId=...` — cheap and idle, same
   shape as `PollForWork` (see "Shape of the solution" above).
2. The caller starts the run as usual
   (`POST /agui/{name}` via `souk-client-sdk`), passing
   `metadata: {"kyok": {"sessionId": "<session_id>"}}`.
3. `api_agui.py::_build_forwarded_props`, seeing `metadata.kyok.
   sessionId`, mints a token via `souk.kyok.issue_kyok_token(run_id,
   session_id, agent_id)` — souk already knows `agent_id` at this point,
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
   the bearer token back into `(run_id, session_id, agent_id)`,
   cross-checks `agent_id` against `souk.broker.get(run_id)`'s live
   `agent_id` (rejecting if the run isn't currently live, or somehow
   belongs to a different agent_id than the token claims — see the next
   section for why this matters even though the second half of that
   check can never actually fire today), then queues the request for
   `session_id` to pick up off `/kyok/poll` and blocks relaying its
   response back once `/kyok/respond/{request_id}` starts arriving.

The token is signed the same way `souk.identity.issue_session_token`
signs provider session tokens (HMAC over a base64 JSON body, same
`settings.token_signing_secret`) — new payload shape, same mechanism, in
`souk/kyok.py`.

## Binding a token to the specific run and provider that hold it

A provider's identity is real — its Ed25519 keypair (`souk_agent_sdk.
identity`), proven at registration and re-proven on every `PollForWork`/
`AgentSession` call via a souk-issued session token (`souk/identity.py`).
But a bearer token by itself only proves souk minted it, not who's
presenting it — a leaked or merely-retained token is just as usable by
whoever holds the string, regardless of whether they can make sense of
its contents (encrypting it so only the right provider could *read* it
wouldn't help either — replaying an opaque blob as a bearer value
doesn't require understanding it). Two separate checks close this, at
two different layers:

**1. The token is bound to a specific run, checked against souk's own
live state.** souk already knows, at the moment it mints a KYOK token,
exactly which `agent_id` the run belongs to (the same `agent_id`
`_run_agent` resolved this whole request against) — so the token says so
explicitly (`issue_kyok_token(run_id, session_id, agent_id)`) instead of
leaving that implicit, and `chat_completions` re-derives the *current*
truth from `souk.broker` and requires them to match. This closes a real
gap: without it, a KYOK token remains a usable bearer credential for its
full `KYOK_TOKEN_TTL_SECONDS` (an hour) even after the run it was minted
for has finished and its provider process is long gone. Checking against
`broker.get(run_id)` (which forgets a run the instant its pipeline
terminates — see `broker.py`'s `_pipeline`) collapses that window to
"exactly as long as the run is genuinely still in flight." The
`agent_id` *mismatch* half of this check can't actually fire yet — a
run's `agent_id` is fixed forever from the moment `repo.create_run`
assigns it — it's there anyway as an explicit, checked invariant rather
than one only true by construction, so it starts doing real work the
moment that assumption ever stops holding (some future run hand-off/
reassignment path), instead of silently going stale.

**2. Every call is signed, live, by the calling provider's own identity
key.** This is the part (1) can't cover — (1) only checks *which run*
the token claims to be for, never *who's actually presenting it right
now*. `souk_agent_sdk.KyokSigningAuth` is an `httpx.Auth` that signs each
outgoing completion request with this provider process's own Ed25519
key over `{bearer token}:{timestamp}:{sha256(body)}`, attached as
`X-Souk-Kyok-Timestamp`/`X-Souk-Kyok-Signature`. `api_llm_bridge.
_verify_caller_identity` looks up the *actual* registered `public_key`
for the token's `agent_id` (`repo.get_agent_public_key` — souk's own
source of truth, same table every other identity check in this system
reads from) and verifies the signature was really produced by it, over
this exact request (the body hash stops a captured signature being
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

### Why opening a bridge session needs no authorization check of its own

`session_id` (`KyokBridge.open()`) is just a locally-generated random
value — souk never issues or vets one. That's fine, deliberately: a
session isn't an identity or a capability grant, it's a rendezvous label
whoever starts a run picks for their own convenience, and `metadata.kyok.
sessionId` is only ever set by whoever calls `POST /agui/{name}` to
*create* that run in the first place. There's no way for a third party
to attach their own session_id to a run they didn't themselves start —
metadata is fixed at `repo.create_run`/`reopen_run` time, not mutable
afterward — and no way to guess someone else's (128 bits of entropy,
never transmitted anywhere outside the caller's own bridge <-> souk
traffic for a run that caller itself started). Opening a session and
having it actually matter both require being the one who started the
run it gets attached to — there's no separable "who's allowed to open
one" question to gate.

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
  doesn't validate what comes back over `/kyok/respond` beyond it being
  well-formed OpenAI-shaped chunks — same trust model as "the provider
  says whatever it wants over AG-UI"; souk is a relay, not a validator of
  LLM output.
- **`CLAIM_TIMEOUT_SECONDS` (30s) is a delay, not an instant failure.**
  If a caller's bridge drops or never polls, the provider's
  `/kyok/v1/chat/completions` call doesn't find out "nobody's listening"
  until this timeout elapses — souk has no way to distinguish "not
  claimed yet" from "will never be claimed" any earlier than that.
