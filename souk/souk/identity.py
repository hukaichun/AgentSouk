"""Minimal provider identity: proves whoever registers under a public_key
actually holds the matching private key, and gates every worker call on a
short-lived bearer token issued only after that proof.

Deliberately not a full account system — no signup flow, no stored
credentials beyond the public key itself. A provider's identity *is* its
Ed25519 keypair (see souk_agent_sdk.identity, which generates and persists
one on first run). This is enough to close the one gap that matters for a
public, multi-tenant souk: nobody can act as a public_key they don't hold
the private key for. A `name` on its own is deliberately *not* an identity —
it is not exclusive, and two providers may both offer `translator`. What is
owned is the pair: `PRIMARY KEY (provider_key, name)` in souk/schema.py.

One check, at one point: registration must be signed with the private key
matching the public_key it presents (see repo.register_agents).

There used to be a second — a bearer token issued at registration and
required on every worker call. It existed for `claim_work`, which a provider
called to ask for work. souk hands work over now (see souk/broker.py), so
nothing asks souk for anything and there is no call left to present a token
to. It was removed rather than left as something for a transport to find a
use for: a credential nobody verifies is one downstream will assume means
more than it does.

Authenticating a *connection* is a different question, with a different
answer, and it belongs to whatever holds connections.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

import jwt
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


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


# A short, stable stand-in for a provider's public key, for places a
# 64-character hex string is unusable — chiefly an address a human reads or
# types (see docs/library-architecture.md on addressing). Derived, never
# minted: anyone holding the key can compute it, it is the same on every
# souk, and there is nothing to store on the provider's side or hand out.
#
# 16 hex is 64 bits. That is not about accidental collisions (those would
# need ~2^32 providers) but deliberate ones: grinding keypairs until one
# matches a chosen provider's fingerprint costs ~2^64, out of reach. souk
# also refuses a registration whose fingerprint already belongs to another
# key (see repo.ensure_provider), so even a found collision blocks a
# registration rather than impersonating anyone.
FINGERPRINT_HEX_LENGTH = 16


def provider_fingerprint(public_key: str) -> str:
    """SHA-256 of the key's raw bytes, truncated. The bytes rather than the
    hex text, so the answer does not depend on how the hex was cased."""
    return hashlib.sha256(bytes.fromhex(public_key)).hexdigest()[:FINGERPRINT_HEX_LENGTH]


def is_fingerprint(value: str) -> bool:
    """Whether this is a fingerprint rather than a full public key. The two
    are distinguishable by length — 16 hex against 64 — so one parameter can
    accept either without ambiguity."""
    return len(value) == FINGERPRINT_HEX_LENGTH


def is_timestamp_fresh(timestamp: int) -> bool:
    return abs(time.time() - timestamp) <= SIGNATURE_FRESHNESS_WINDOW_SECONDS


# What a signed request is *for*. Every signing payload starts with one of
# these, so a signature captured for one operation cannot be presented as
# another.
#
# This is not defensive habit; it closes a hole that was measured. Before it,
# a registration signed `"translator:1755300000"` — and the obvious payload
# for deleting one agent is the same name and the same timestamp, byte for
# byte. Anyone who merely observed a provider register a single agent held a
# valid order to delete it, for as long as the freshness window allows.
#
# The hole predates deletion: with only one signed operation there was nothing
# to be confused with. Adding a second is what made it reachable, which is why
# the prefixes arrive with it rather than after.
_REGISTER = "souk-register"
_DELETE_AGENT = "souk-delete-agent"


def registration_signing_payload(agent_names: list[str], timestamp: int) -> bytes:
    """What a registration signs: which names are being claimed, and when.

    The identity itself is not in here and does not need to be — the
    signature is verified against the public_key presented alongside, so a
    captured payload cannot be re-presented under a different key. What must
    be covered is what the request *claims* (the names), which operation it
    is asking for, and its freshness.
    """
    return f"{_REGISTER}:{','.join(sorted(agent_names))}:{timestamp}".encode()


def agent_deletion_signing_payload(agent_name: str, timestamp: int) -> bytes:
    """What deleting one agent signs.

    One name, never a batch: deleting is not something to do by accident to a
    list. Registration is declarative and idempotent; this is neither.
    """
    return f"{_DELETE_AGENT}:{agent_name}:{timestamp}".encode()


# How long one freshly-signed hop stays usable. Only the last hop's expiry
# is enforced (see verify_actor_chain), so this bounds "who is using this
# chain right now", not how long the provenance it records stays readable.
ACTOR_CHAIN_TTL_SECONDS = 300


def _sign_hop(private_key: Ed25519PrivateKey, subject: dict, prev_token: str | None) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "subject": subject,
            "actorPublicKey": private_key.public_key().public_bytes_raw().hex(),
            "prevHash": _hop_hash(prev_token) if prev_token is not None else None,
            "iat": now,
            "exp": now + ACTOR_CHAIN_TTL_SECONDS,
        },
        private_key,
        algorithm="EdDSA",
    )


def new_actor_chain(private_key: Ed25519PrivateKey, subject: dict) -> list[str]:
    """Start a chain. `subject` is who it is fundamentally about.

    An agent calling on its own behalf passes itself
    (`{"type": "agent", "publicKey": ...}`). One that authenticated a human
    by its own means — SSO, an internal login, whatever souk has no view of
    — passes that instead (`{"type": "user", "id": "employee_x"}`). souk
    never verifies that claim, because it cannot: how a user proved
    themselves to the first agent is between them. What souk verifies is
    that every actor signing a hop really holds the key it claims, and that
    the chain has not been altered since.
    """
    return [_sign_hop(private_key, subject, None)]


def extend_actor_chain(private_key: Ed25519PrivateKey, prev_chain: list[str]) -> list[str]:
    """Add this actor's hop to a chain it received and is relaying onward.

    This is what makes provenance survive a delegation. Without it a hop is
    a dead end: whoever receives the call can be told who is calling *now*,
    but not on whose behalf, and nothing ties the two together. souk keeps
    this in core precisely so that an agent running inside souk can carry a
    chain forward exactly as a remote one does — an in-process hop must not
    be the place a chain quietly stops.

    The subject is copied from the last hop as-is and not verified here;
    souk checks the whole chain's integrity when it is used.
    """
    if not prev_chain:
        raise ValueError(
            "extend_actor_chain requires a non-empty prev_chain — use new_actor_chain to originate one"
        )
    subject = jwt.decode(prev_chain[-1], options={"verify_signature": False})["subject"]
    return [*prev_chain, _sign_hop(private_key, subject, prev_chain[-1])]


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
    is built, and protocols.a2a's _start_run for where this is called.

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


# ---- souk's own identity
#
# The other end of everything above. A provider is its keypair and proves that
# on demand; until now souk held no key at all and proved nothing back, so the
# relationship was one-directional and a provider had no way to tell one souk
# from another. This is souk's half, and it is deliberately the same shape as
# the provider's — a private key, a public half to publish, and a signature
# over bytes somebody else chose.


class SoukIdentity:
    """The keypair one souk is, when it has been given one.

    Constructed from configuration rather than generated, and never generated
    here: see `CoreSettings.identity_private_key` for why a souk that mints
    its own on startup is worse than a souk with no identity at all.
    """

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._private_key = private_key
        self.public_key = private_key.public_key().public_bytes_raw().hex()

    @classmethod
    def from_hex(cls, private_key_hex: str) -> "SoukIdentity":
        """Load from a hex-encoded 32-byte seed.

        Raises `ValueError` on anything that is not one, at construction —
        a souk configured with a malformed key should fail to start rather
        than fail the first provider that asks it to prove itself.
        """
        try:
            raw = bytes.fromhex(private_key_hex)
        except ValueError as e:
            raise ValueError("identity_private_key is not valid hex") from e
        if len(raw) != 32:
            raise ValueError(
                f"identity_private_key must be a 32-byte seed (64 hex chars), got {len(raw)} bytes"
            )
        return cls(Ed25519PrivateKey.from_private_bytes(raw))

    @staticmethod
    def generate_hex() -> str:
        """A fresh key, hex-encoded, for whoever is provisioning one.

        A helper for operators — `python -c "from souk.identity import
        SoukIdentity; print(SoukIdentity.generate_hex())"` — and deliberately
        not called by souk itself. Generating is a provisioning act with a
        secret to store; doing it implicitly at startup is what produces a
        souk whose identity changes under its providers.
        """
        return Ed25519PrivateKey.generate().private_bytes_raw().hex()

    def sign(self, payload: bytes) -> str:
        """Sign arbitrary bytes. The mirror of `verify_signature` above, and
        of `ProviderIdentity.sign` on the other side.

        What gets signed is not souk's to decide: proving identity when a
        connection opens is a serving act, so the payload belongs to whoever
        is serving souk (see docs/library-architecture.md on the boundary).
        Core supplies the primitive.
        """
        return self._private_key.sign(payload).hex()


