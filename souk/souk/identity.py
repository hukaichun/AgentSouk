"""Minimal provider identity: proves whoever registers under a public_key
actually holds the matching private key, and gates gRPC calls on a
short-lived bearer token issued only after that proof.

Deliberately not a full account system — no signup flow, no stored
credentials beyond the public key itself. A provider's identity *is* its
Ed25519 keypair (see souk_agent_sdk.identity, which generates and persists
one on first run). This is enough to close the one gap that matters for a
public, multi-tenant souk: nobody can act as a public_key they don't hold
the private key for. `name` (the human-facing label an agent registers
under) is deliberately *not* part of the ownership model — it isn't unique
or exclusive (see souk/schema.py's UNIQUE(public_key, name)); the souk-assigned
agent_id, scoped to (public_key, name), is what's actually owned.

Two independent checks, at two different points:
  1. Registration (register_agents / verify_registration_signature):
     the request must be signed with the private key matching the
     public_key it presents (see repo.register_agents). Expensive-ish
     (Ed25519 verify), so it only happens at registration, not on every
     poll.
  2. Every gRPC call (PollForWork/AgentSession): must present a bearer
     token issued by step 1 (see issue_session_token/verify_session_token).
     Cheap (HMAC verify), stateless (no session store — the token itself
     carries sdk_client_id + an expiry, signed so it can't be forged
     without token_signing_secret).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

import jwt
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


SESSION_TOKEN_TTL_SECONDS = 3600

# How far a signed request's own `timestamp` is allowed to drift from
# souk's clock before the signature is refused outright, independent of
# whether it's cryptographically valid. Without this, a signature has no
# concept of "when" — anyone who merely *observes* one valid signed
# request on the wire (no need to break Ed25519 or steal a private key)
# could replay the exact same bytes indefinitely, e.g. to keep minting
# fresh session tokens for a provider's identity forever. This bounds
# that to a narrow window instead. Transport encryption (TLS) is what
# stops the observation in the first place — this is defense in depth,
# not a substitute for it.
SIGNATURE_FRESHNESS_WINDOW_SECONDS = 60


def is_timestamp_fresh(timestamp: int) -> bool:
    return abs(time.time() - timestamp) <= SIGNATURE_FRESHNESS_WINDOW_SECONDS


def registration_signing_payload(sdk_client_id: str, agent_names: list[str], timestamp: int) -> bytes:
    return f"{sdk_client_id}:{','.join(sorted(agent_names))}:{timestamp}".encode()


@dataclass
class ChainResult:
    subject: dict
    # Ordered oldest -> newest, mirroring the input chain: actor_public_keys[0]
    # is whoever originated the chain, actor_public_keys[-1] is the immediate
    # caller souk received this request from.
    actor_public_keys: list[str]


class InvalidActorChain(ValueError):
    pass


def _hop_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def verify_actor_chain(chain: list[str]) -> ChainResult:
    """Verifies a caller-supplied identity chain — see
    souk_agent_sdk.identity.new_actor_chain/extend_actor_chain for how one
    is built, and api_a2a._start_run for where this is called.

    Each entry in `chain` is a standard compact JWT (alg=EdDSA), signed by
    whoever performed that hop, so any off-the-shelf JWT library can
    decode/verify an individual entry — this is deliberately *not* an
    invented wire format of our own. What's souk-specific is only how
    entries are chained together: each entry's payload carries
    `prevHash` = sha256 of the previous entry's raw token string (or None
    for the first), binding them into an ordered, tamper-evident sequence
    that can't be reordered, truncated, or spliced with hops from a
    different chain. This is intentionally simpler than RFC 8693 OAuth
    Token Exchange's nested `act` claim, which assumes a central token
    issuer re-signing at every hop — souk has no such issuer; each actor
    signs for itself instead.

    `subject` (who the chain is fundamentally about — see each hop's
    payload) is carried unchanged through every entry and must match
    across all of them; it does NOT have to correspond to a registered
    souk identity or even to any of the actors that signed a hop — e.g.
    an "agency" agent can originate a chain asserting
    `subject={"type": "user", "id": "..."}` for a human it authenticated
    by whatever means are its own business, and souk has no way to (and
    makes no attempt to) verify that claim independently. What souk *does*
    cryptographically verify is that every actor in the chain really is
    who it claims (matching the same Ed25519-keypair-is-identity model as
    provider registration), and that the chain hasn't been tampered with.

    Only the *last* hop's `exp` is enforced — it's the one that represents
    "who is actually using this chain to make this call right now" and
    needs freshness. Earlier hops are historical provenance, not standing
    authorization: their signatures are still fully verified (a hop can't
    be forged or altered), but letting their `exp` lapse must not brick
    the whole chain — a run that's been paused on `input-required` for
    longer than a hop's TTL (see souk.pause) still needs to be able to
    resume and have its provider extend the chain further, and a chain
    built once at the start of a long-running delegation shouldn't expire
    out from under normal thinking/tool-call latency either.

    Raises InvalidActorChain with a human-readable reason on any failure;
    never returns a partially-verified result.
    """
    if not chain:
        raise InvalidActorChain("empty actor chain")

    subject: dict | None = None
    actor_public_keys: list[str] = []
    prev_token: str | None = None

    for i, token in enumerate(chain):
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError as e:
            raise InvalidActorChain(f"hop {i}: unparseable token: {e}") from e

        actor_public_key = unverified.get("actorPublicKey")
        if not isinstance(actor_public_key, str):
            raise InvalidActorChain(f"hop {i}: missing actorPublicKey")
        try:
            public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(actor_public_key))
        except ValueError as e:
            raise InvalidActorChain(f"hop {i}: malformed actorPublicKey: {e}") from e

        is_last_hop = i == len(chain) - 1
        try:
            payload = jwt.decode(
                token,
                key=public_key,
                algorithms=["EdDSA"],
                options={"verify_exp": is_last_hop},
            )
        except jwt.PyJWTError as e:
            reason = "signature/expiry check failed" if is_last_hop else "signature check failed"
            raise InvalidActorChain(f"hop {i}: {reason}: {e}") from e

        expected_prev_hash = _hop_hash(prev_token) if prev_token is not None else None
        if payload.get("prevHash") != expected_prev_hash:
            raise InvalidActorChain(f"hop {i}: prevHash doesn't match — chain reordered, truncated, or spliced")

        if i == 0:
            subject = payload.get("subject")
            if not isinstance(subject, dict):
                raise InvalidActorChain("hop 0: missing subject")
        elif payload.get("subject") != subject:
            raise InvalidActorChain(f"hop {i}: subject changed partway through the chain")

        actor_public_keys.append(actor_public_key)
        prev_token = token

    assert subject is not None
    return ChainResult(subject=subject, actor_public_keys=actor_public_keys)


def verify_signature(public_key_hex: str, signature_hex: str, payload: bytes) -> bool:
    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        public_key.verify(bytes.fromhex(signature_hex), payload)
        return True
    except (InvalidSignature, ValueError):
        return False


def issue_session_token(sdk_client_id: str, signing_secret: str) -> str:
    body = base64.urlsafe_b64encode(
        json.dumps({"sdk_client_id": sdk_client_id, "exp": int(time.time()) + SESSION_TOKEN_TTL_SECONDS}).encode()
    ).decode()
    signature = hmac.new(signing_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def verify_session_token(token: str, signing_secret: str) -> str | None:
    """Returns the token's sdk_client_id if valid (correct signature, not
    expired), else None. Called on every PollForWork/AgentSession — see
    souk.grpc_server.
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
    sdk_client_id = payload.get("sdk_client_id")
    return sdk_client_id if isinstance(sdk_client_id, str) else None
