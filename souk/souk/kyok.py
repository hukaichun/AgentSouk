"""Keep Your Own Key: run-scoped bearer tokens for souk's
OpenAI-compatible LLM bridge (see docs/keep-your-own-key.md for the full
picture).

A KYOK token names one run, because the only thing a completion call
needs from the "api_key" an agent provider sends it is "which run is
this for" — the LLM provider answering it is found from the run (the
caller named one at run start; see KyokRelay below), never from anything
in the token. It is an HMAC over a base64 JSON body under
`settings.token_signing_secret` — the same mechanism souk.identity once
used for provider session tokens; that one is gone with the call it
guarded, so this is now the only thing that secret signs.

Also carries the agent — souk already knows, at the moment it mints this
token (protocols.agui's build_forwarded_props, called with the run's own
pair), exactly which provider identity this run belongs to; the token
says so explicitly rather than leaving that implicit. See protocols.kyok's
KyokAdapter.complete for where this gets checked against souk.broker's
live view of who is actually running run_id right now — a provider's
identity is real (its Ed25519 keypair) even though this HTTP endpoint
itself, unlike a worker's own calls, carries no bearer proving it on the
wire (staying OpenAI-wire-compatible rules that out) — this is how the
binding still happens without needing one.

**Signed, not sealed.** Everything in the body is readable by whoever
holds the token, which is the agent provider — the one party KYOK exists
to keep the caller's key away from. So nothing goes in here that it must
not learn. The token used to carry a session routing key for exactly
that reason (a hash, because the id itself was probed and abused); the
whole rendezvous concept is gone now — completions route to a
registered, identity-holding LLM provider, so there is no caller-side
secret left to protect or to hash.

Wire shapes are OpenAI's and come from the `openai` package — types
only, no client is ever constructed here; souk hand-writes no completion
field name (the same rule that put `ag-ui-protocol` and `a2a-sdk` in the
dependency list).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

from openai.types.chat import ChatCompletionChunk, CompletionCreateParams
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from souk.models import AgentRef, LlmRef

# Deliberately short: a KYOK token only needs to live from "souk minted
# it into this run's forwardedProps" to "the provider's last completion
# call for this run" — not the whole lifetime of a possibly-long-running
# run. Provider code that holds a run open longer than this for its LLM
# calls would need a longer TTL; not a case that's come up yet.
KYOK_TOKEN_TTL_SECONDS = 3600


@dataclass
class KyokToken:
    run_id: str
    agent: AgentRef


def issue_kyok_token(run_id: str, agent: AgentRef, signing_secret: str) -> str:
    body = base64.urlsafe_b64encode(
        json.dumps(
            {
                "runId": run_id,
                "providerKey": agent.provider_key,
                "agentName": agent.name,
                "exp": int(time.time()) + KYOK_TOKEN_TTL_SECONDS,
            }
        ).encode()
    ).decode()
    signature = hmac.new(signing_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def verify_kyok_token(token: str, signing_secret: str) -> KyokToken | None:
    """Returns the decoded (run_id, agent) if `token` is a well-formed,
    correctly-signed, unexpired KYOK token, else None. Called on every
    completion call — see protocols.kyok's KyokAdapter.complete, which
    additionally checks the returned agent against souk.broker's live
    record of who's running run_id right now; this function only checks
    the token is genuinely souk's own and hasn't expired.
    """
    try:
        body, signature = token.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(signing_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body.encode()))
    except (ValueError, UnicodeDecodeError):
        return None
    if payload.get("exp", 0) < time.time():
        return None
    run_id = payload.get("runId")
    provider_key = payload.get("providerKey")
    agent_name = payload.get("agentName")
    if not all(isinstance(v, str) for v in (run_id, provider_key, agent_name)):
        return None
    return KyokToken(
        run_id=run_id,
        agent=AgentRef(provider_key=provider_key, name=agent_name),
    )


class KyokOptIn(BaseModel):
    """`metadata.kyok`, as a type instead of an isinstance chain.

    souk defines this shape — which offering answers the run's LLM calls,
    and the caller's opaque credential to it — so souk states it once,
    here, and parses with it. Metadata as a whole stays free-form by
    contract; only souk's own corner of it gets a schema. `extra="allow"`
    keeps that promise: unknown keys under `kyok` are somebody else's
    business, not a validation error.

    Parsing failure means "no opt-in", not "bad request" — see
    `parse_kyok_opt_in` — because a metadata key souk happens to also use
    is not a claim the caller must have meant souk's schema by it.
    """

    model_config = ConfigDict(frozen=True, extra="allow", populate_by_name=True)

    llm_provider: LlmRef | None = Field(default=None, alias="llmProvider")
    context: Any = None


def parse_kyok_opt_in(metadata: dict) -> KyokOptIn | None:
    """The typed read of `metadata.kyok`; None when absent or not this
    shape. `LlmRef` uses snake_case field names while the wire uses
    camelCase — the alias on the model covers the outer key, and the pair
    itself arrives as {"providerKey", "name"}, so it is re-keyed here,
    the one place both spellings are known."""
    raw = metadata.get("kyok")
    if not isinstance(raw, dict):
        return None
    target = raw.get("llmProvider")
    if isinstance(target, dict):
        raw = {**raw, "llmProvider": {"provider_key": target.get("providerKey"), "name": target.get("name")}}
    try:
        return KyokOptIn.model_validate(raw)
    except ValidationError:
        return None


def strip_kyok_context(metadata: dict) -> dict:
    """Metadata with the caller's KYOK credential removed — the one part
    of `metadata.kyok` that must never reach anything that persists (see
    KyokBinding on why). Both run-creating paths call this before writing
    anything, and it lives here so the shape knowledge has one home."""
    kyok = metadata.get("kyok")
    if isinstance(kyok, dict) and "context" in kyok:
        return {**metadata, "kyok": {k: v for k, v in kyok.items() if k != "context"}}
    return metadata


class KyokForwardedProps(BaseModel):
    """What souk plants under `forwardedProps.kyok` for the agent
    provider: the run's token, and nothing else. A model rather than a
    dict literal so both mint points (protocols.agui and protocols.a2a's
    delegation inheritance) and every reader state the same shape —
    souk-defined shapes do not travel as anonymous dicts.
    """

    model_config = ConfigDict(frozen=True)

    token: str


def kyok_forwarded_props(run_id: str, agent: AgentRef, signing_secret: str) -> dict[str, Any]:
    """The `kyok` entry for a run's forwardedProps, built through the
    model at both mint points."""
    return KyokForwardedProps(
        token=issue_kyok_token(run_id, agent, signing_secret)
    ).model_dump()


def read_kyok_forwarded_props(forwarded_props: Any) -> KyokForwardedProps | None:
    """The reader's half: a provider-side (or test) look at
    `forwardedProps.kyok`, through the same model that wrote it. None when
    the run carries no KYOK grant."""
    if not isinstance(forwarded_props, dict):
        return None
    raw = forwarded_props.get("kyok")
    if raw is None:
        return None
    try:
        return KyokForwardedProps.model_validate(raw)
    except ValidationError:
        return None


@dataclass(frozen=True)
class KyokBinding:
    """Everything a run's KYOK opt-in established, held together because it
    has one lifetime — the run's.

    `context` is the credential the caller presented *to the LLM provider*
    (`metadata.kyok.context`): opaque to souk, in the LLM provider's own
    vocabulary, and the reason a provider serving many users can tell
    whose budget a run spends. It lives here and nowhere else — never in
    the runs table, because run metadata comes back verbatim through the
    deliberately-unauthenticated thread endpoints and the agent provider
    holds a thread_id; persisting it would hand the one party KYOK
    defends against the caller's credential, the exact shape of the
    session-id disclosure this design replaced. protocols.agui and
    protocols.a2a both strip it before anything is written.

    `actor_chain` is the raw, hop-signed JWT chain that reached this run,
    already verified by souk at run start. Raw rather than souk's digest
    of it: each hop is signed by a registered provider key, so the LLM
    provider can verify the delegation path itself instead of taking
    souk's word — and policy like "serve only chains through providers I
    expect" needs nothing more.
    """

    llm_provider: LlmRef
    context: Any = None
    actor_chain: list[str] | None = None


@dataclass(frozen=True)
class CompletionRequest:
    """One completion, with everything its LLM provider may base policy on.

    souk hands over context and decides nothing — whether to serve, to
    throttle, or to bill is the LLM provider's own business (souk never
    decides on a provider's behalf, and that invariant cuts both ways).
    `agent` is the agent provider making this call, already proven by the
    call-time signature by the time this object exists; `context` and
    `actor_chain` come from the run's binding (see KyokBinding). souk has
    no user concept and none is smuggled in here: if the caller wants the
    LLM provider to know who it is, that is what `context` carries, in
    their shared vocabulary.
    """

    run_id: str
    agent: AgentRef
    body: CompletionCreateParams
    # Which of this LLM provider's own offerings was addressed — one
    # connection may serve several names (a fast tier and a smart one),
    # and the name is how its handler tells them apart.
    llm_name: str = ""
    context: Any = None
    actor_chain: list[str] | None = None


class ConnectedLLMProvider(Protocol):
    """Whoever is serving completions under a registered LLM-provider
    name, as the relay sees them — the KYOK counterpart of
    broker.ConnectedProvider, for the same reason: what carries the
    answer (a call in this process, a frame on a socket) is a
    serving-layer choice this module must not encode.

    Two members, because souk asks exactly two things: who this is, and
    answer this completion. Chunks are always streaming-shaped
    regardless of what the agent provider asked for — collapsing for a
    non-streaming caller is souk's job (protocols.kyok.collapse_stream),
    done once rather than in every implementation.

    Failure — including a policy refusal — is an exception, not a chunk:
    OpenAI's stream has no error chunk type, and inventing one would be
    a hand-written wire shape. Whoever relays the stream onward decides
    what an error looks like on that wire (protocols.kyok's
    CompletionRelay).
    """

    # The LLM provider's Ed25519 public key, established when it
    # attached — the same identity it registered its name under.
    public_key: str

    def complete(self, request: CompletionRequest) -> AsyncIterator[ChatCompletionChunk]: ...


class KyokRelay:
    """The second broker, for real this time: which LLM provider each
    run's completions go to, and which of those providers is reachable
    right now. Same single-process, in-memory assumption as
    broker.RunBroker (see its module docstring), for the same reason:
    none of this needs to survive a restart.

    Two maps, and their lifetimes are the whole design:

    - `_bindings` (run_id → KyokBinding): bound by protocols.agui when a
      run starts with `metadata.kyok.llmProvider`, inherited souk-side
      down A2A delegation (see `inherit` — the caller's context must
      never transit an agent provider), dropped through RunBroker's
      forget funnel — the one path every run ending crosses — which
      Souk.__init__ wires to `discard`. Not lazy cleanup: the registry
      this design replaced reclaimed nothing, and 100k entries it had no
      reason to keep retained 81 MiB. Every key is souk-minted
      (`repo.create_run`), so no outside party can grow this map.
    - `_links` (LlmRef → connection): exists exactly while that offering's
      identity is attached (`attach`/`detach`, mirroring RunBroker's
      provider map — keyed by the pair, because the pair is the address).
      Resolution happens per completion, never at bind time, so a
      provider that drops and re-attaches mid-run is found fresh — the
      binding names an offering, not a connection.
    """

    def __init__(self) -> None:
        self._bindings: dict[str, KyokBinding] = {}
        self._links: dict[LlmRef, ConnectedLLMProvider] = {}

    # ---- run → binding

    def bind_run(self, run_id: str, binding: KyokBinding) -> None:
        self._bindings[run_id] = binding

    def binding_for(self, run_id: str) -> KyokBinding | None:
        return self._bindings.get(run_id)

    def inherit(self, parent_run_id: str, child_run_id: str, actor_chain: list[str] | None) -> bool:
        """Copies a delegating run's binding to the run it spawned — same
        offering, same caller context — with the *child's* own verified
        chain, because the chain in a binding describes the path to that
        run. souk does this itself so the context never passes through
        the delegating agent's hands; an agent that had to copy
        `metadata.kyok` forward would be an agent holding the caller's
        credential, which is the session-id disclosure with a new face.

        Returns whether there was anything to inherit — the caller uses
        that to decide whether the child run gets a KYOK token at all.
        """
        parent = self._bindings.get(parent_run_id)
        if parent is None:
            return False
        self._bindings[child_run_id] = KyokBinding(
            llm_provider=parent.llm_provider,
            context=parent.context,
            actor_chain=actor_chain,
        )
        return True

    def discard(self, run_id: str) -> None:
        """Idempotent, because the forget funnel calls it for every run
        ending, KYOK or not — absence is the common case, not an error."""
        self._bindings.pop(run_id, None)

    # ---- offering → connection

    def attach(self, mapping: dict[LlmRef, ConnectedLLMProvider]) -> None:
        self._links.update(mapping)

    def detach(self, public_key: str) -> None:
        """By identity, not by offering: the party leaving knows who it
        is, and asking it to also remember every name it serves is how
        stale entries happen."""
        for ref in [r for r in self._links if r.provider_key == public_key]:
            del self._links[ref]

    def serving(self, ref: LlmRef) -> ConnectedLLMProvider | None:
        return self._links.get(ref)

    def serving_any(self, public_key: str) -> bool:
        """Whether this identity has any offering attached — what detach
        checks so a no-op departure doesn't announce a roster change."""
        return any(ref.provider_key == public_key for ref in self._links)
