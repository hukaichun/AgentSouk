from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

import jwt
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


SESSION_TOKEN_TTL_SECONDS = 3600

SIGNATURE_FRESHNESS_WINDOW_SECONDS = 60


FINGERPRINT_HEX_LENGTH = 16


def provider_fingerprint(public_key: str) -> str:
    """Derives a short, deterministic identifier for a provider from its public key.

    Two different providers registering under the same fingerprint is treated
    as a collision (see `ProviderFingerprintTaken`); an agent can be resolved
    by either its full public key or this fingerprint.
    """
    return hashlib.sha256(bytes.fromhex(public_key)).hexdigest()[:FINGERPRINT_HEX_LENGTH]


def is_fingerprint(value: str) -> bool:
    return len(value) == FINGERPRINT_HEX_LENGTH


def is_timestamp_fresh(timestamp: int) -> bool:
    return abs(time.time() - timestamp) <= SIGNATURE_FRESHNESS_WINDOW_SECONDS


_REGISTER = "souk-register"
_REGISTER_LLM = "souk-register-llm"
_DELETE_AGENT = "souk-delete-agent"
_DELETE_LLM = "souk-delete-llm"
_KYOK_CALL = "souk-kyok-call"


def _roster_registration_payload(tag: str, names: list[str], timestamp: int) -> bytes:
    """One payload shape for registering a roster of served names: sorted (order-independent), joined with `timestamp`, under a domain `tag`.

    The tag is what keeps the payload spaces apart — a signature for one
    roster (or a deletion order) must not be replayable as another.
    `souk_provider_sdk.identity.roster_registration_payload` computes this
    same shape independently on the provider side and both must agree
    byte-for-byte.
    """
    return f"{tag}:{','.join(sorted(names))}:{timestamp}".encode()


def registration_signing_payload(agent_names: list[str], timestamp: int) -> bytes:
    """Builds the canonical bytes a provider must sign to prove it holds the key it registers with: `_roster_registration_payload` under the agent tag."""
    return _roster_registration_payload(_REGISTER, agent_names, timestamp)


def llm_registration_signing_payload(names: list[str], timestamp: int) -> bytes:
    """Builds the canonical bytes an LLM provider must sign to register `names`: `_roster_registration_payload` under the LLM tag.

    `souk_llm_provider_sdk` computes this same payload independently on the
    provider side and both must agree byte-for-byte.
    """
    return _roster_registration_payload(_REGISTER_LLM, names, timestamp)


def agent_deletion_signing_payload(agent_name: str, timestamp: int) -> bytes:
    """Builds the canonical bytes a provider must sign to authorize deleting one of its agents.

    Uses a distinct domain tag from `registration_signing_payload` so a
    captured registration signature can't be replayed to delete the agent.
    """
    return f"{_DELETE_AGENT}:{agent_name}:{timestamp}".encode()


def llm_deletion_signing_payload(name: str, timestamp: int) -> bytes:
    """Builds the canonical bytes an LLM provider must sign to authorize deleting one of its offerings.

    The LLM mirror of `agent_deletion_signing_payload`, under its own domain
    tag for the same reason. `souk_llm_provider_sdk.llm_deletion_payload`
    computes this same payload independently on the provider side and both
    must agree byte-for-byte.
    """
    return f"{_DELETE_LLM}:{name}:{timestamp}".encode()


def kyok_call_signing_payload(bearer: str, timestamp: int, body_hash: str) -> bytes:
    """Builds the canonical bytes the agent provider signs to prove it made a given KYOK completion call.

    Binds the payload to the bearer token, timestamp, and a hash of the
    request body, so a captured signature can't be replayed for a
    different call. `souk_provider_sdk.identity.kyok_call_payload`
    computes this same payload independently on the provider side and
    both must agree byte-for-byte.
    """
    return f"{_KYOK_CALL}:{bearer}:{timestamp}:{body_hash}".encode()


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
    """Starts a new actor chain: a single hop, signed by `private_key`, vouching for `subject`."""
    return [_sign_hop(private_key, subject, None)]


def extend_actor_chain(private_key: Ed25519PrivateKey, prev_chain: list[str]) -> list[str]:
    """Appends a new hop signed by `private_key` to `prev_chain`, carrying the same subject forward.

    The new hop links to the chain's last hop via a hash of that token, so
    the chain records who acted on whose behalf, in order. Raises
    `ValueError` if `prev_chain` is empty.
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
    actor_public_keys: list[str]


class InvalidActorChain(ValueError):
    pass


def _hop_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def verify_actor_chain(chain: list[str]) -> ChainResult:
    """Verifies an actor chain and returns the subject it vouches for plus each hop's actor key, in order.

    Each hop's signature must verify under its own embedded public key, and
    each hop after the first must link to the previous hop via a matching
    `prevHash` — reordering, truncating, or splicing in a hop from a
    different chain breaks this and is rejected. Every hop must vouch for
    the same `subject`. Only the last hop's expiry is enforced; earlier
    hops may have expired since they were signed. Raises
    `InvalidActorChain` on any of these failures, including an empty chain
    or an unparseable/forged token.

    Hops also arrive from out-of-process providers:
    `souk_provider_sdk.identity.ProviderIdentity.sign_hop` builds the same
    JWT claim format independently, and any change here must keep verifying
    what it signs.
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
    """Returns True if `signature_hex` is a valid Ed25519 signature by `public_key_hex` over `payload`.

    Returns False (never raises) for a mismatched key, a mismatched
    payload, or malformed hex in either the key or the signature.
    """
    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        public_key.verify(bytes.fromhex(signature_hex), payload)
        return True
    except (InvalidSignature, ValueError):
        return False


class SoukIdentity:
    """A souk instance's own signing identity, distinct from any provider's.

    Two instances built from different keys are different identities that
    don't verify each other's signatures; the same key hex produces the
    same public identity across restarts, letting a provider pin souk by
    its public key.
    """

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._private_key = private_key
        self.public_key = private_key.public_key().public_bytes_raw().hex()

    @classmethod
    def from_hex(cls, private_key_hex: str) -> "SoukIdentity":
        """Builds an identity from a 32-byte seed given as 64 hex chars.

        Raises `ValueError` if the string isn't valid hex or doesn't
        decode to exactly 32 bytes.
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
        """Generates a fresh private key and returns it as a 32-byte hex seed."""
        return Ed25519PrivateKey.generate().private_bytes_raw().hex()

    def sign(self, payload: bytes) -> str:
        """Signs `payload` with this identity's private key, returning the signature as hex."""
        return self._private_key.sign(payload).hex()


