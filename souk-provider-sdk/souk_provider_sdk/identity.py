from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

import jwt
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ACTOR_CHAIN_TTL_SECONDS = 300

_REGISTER = "souk-register"
_DELETE_AGENT = "souk-delete-agent"
_KYOK_CALL = "souk-kyok-call"


def verify_signature(public_key_hex: str, signature_hex: str, payload: bytes) -> bool:
    """Returns True iff `signature_hex` is a valid Ed25519 signature over `payload` for `public_key_hex`.

    Returns False (never raises) for a bad signature, a mismatched payload, a key from
    another identity, or malformed hex/length input.
    """
    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        public_key.verify(bytes.fromhex(signature_hex), payload)
        return True
    except (InvalidSignature, ValueError):
        return False


def roster_registration_payload(tag: str, names: list[str], timestamp: int) -> bytes:
    """One payload shape for registering any roster of served names: sorted (order-independent), joined with `timestamp`, under a domain `tag`.

    Both rosters sign these bytes — agents under `souk-register`, LLM
    offerings under `souk-register-llm` — and the tag is what keeps a
    signature for one roster unreplayable as the other.
    """
    return f"{tag}:{','.join(sorted(names))}:{timestamp}".encode()


def sign_roster_registration(
    identity: "ProviderIdentity", tag: str, names: list[str], timestamp: int | None = None
) -> tuple[str, int]:
    """Signs a roster registration payload, defaulting the timestamp to now.

    Returns the `(signature, timestamp)` pair actually signed over, so callers
    can send both to the verifier.
    """
    timestamp = int(time.time()) if timestamp is None else timestamp
    return identity.sign(roster_registration_payload(tag, names, timestamp)), timestamp


def registration_payload(agent_names: list[str], timestamp: int) -> bytes:
    """Builds the canonical bytes signed for registering agents: `roster_registration_payload` under the agent tag."""
    return roster_registration_payload(_REGISTER, agent_names, timestamp)


def deletion_payload(agent_name: str, timestamp: int) -> bytes:
    """Builds the canonical bytes signed to delete a single agent, distinct from a registration payload."""
    return f"{_DELETE_AGENT}:{agent_name}:{timestamp}".encode()


def kyok_call_payload(bearer: str, timestamp: int, body_hash: str) -> bytes:
    return f"{_KYOK_CALL}:{bearer}:{timestamp}:{body_hash}".encode()


class ProviderIdentity:
    """An Ed25519 keypair identifying a provider; `public_key` is its 64-char hex-encoded public key."""

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._private_key = private_key
        self.public_key = private_key.public_key().public_bytes_raw().hex()

    @classmethod
    def generate(cls) -> "ProviderIdentity":
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def load_or_create(cls, path: str | Path) -> "ProviderIdentity":
        """Loads the private key at `path` if it exists, else generates one and writes it there (mode 0600).

        Calling this again with the same path yields an identity with the same `public_key`, so a
        restarted process keeps its identity.
        """
        path = Path(path)
        if path.exists():
            return cls(Ed25519PrivateKey.from_private_bytes(path.read_bytes()))
        identity = cls.generate()
        path.write_bytes(
            identity._private_key.private_bytes_raw()  # noqa: SLF001 - own field
        )
        path.chmod(0o600)
        return identity

    def sign(self, payload: bytes) -> str:
        """Signs arbitrary bytes and returns the hex-encoded Ed25519 signature."""
        return self._private_key.sign(payload).hex()

    def sign_registration(self, agent_names: list[str], timestamp: int | None = None) -> tuple[str, int]:
        """Signs `registration_payload(agent_names, timestamp)` (current time if `timestamp` is None) and returns (signature_hex, timestamp)."""
        return sign_roster_registration(self, _REGISTER, agent_names, timestamp)

    def sign_deletion(self, agent_name: str, timestamp: int | None = None) -> tuple[str, int]:
        timestamp = int(time.time()) if timestamp is None else timestamp
        return self._private_key.sign(deletion_payload(agent_name, timestamp)).hex(), timestamp


    def sign_hop(
        self, subject: dict, prev_token: str | None = None, ttl: int = ACTOR_CHAIN_TTL_SECONDS
    ) -> str:
        """Issues a JWT (EdDSA) hop binding `subject` to this identity's public key, optionally chained to `prev_token` via its sha256 in `prevHash`, expiring after `ttl` seconds.

        `souk.identity.verify_actor_chain` is the verifier these hops must
        satisfy; it builds the same claim format independently, and any
        change here must stay verifiable by it.
        """
        now = int(time.time())
        return jwt.encode(
            {
                "subject": subject,
                "actorPublicKey": self.public_key,
                "prevHash": hashlib.sha256(prev_token.encode()).hexdigest()
                if prev_token is not None
                else None,
                "iat": now,
                "exp": now + ttl,
            },
            self._private_key,
            algorithm="EdDSA",
        )

    def new_chain(self, subject: dict) -> list[str]:
        """Starts a new actor chain: a one-element list holding a single signed hop for `subject`."""
        return [self.sign_hop(subject)]

    def extend_chain(self, prev_chain: list[str]) -> list[str]:
        """Appends a hop signed by this identity, carrying forward the subject of the chain's last hop.

        Raises ValueError if `prev_chain` is empty — use `new_chain` to start one.
        """
        if not prev_chain:
            raise ValueError("extend_chain requires a non-empty chain — use new_chain to start one")
        subject = jwt.decode(prev_chain[-1], options={"verify_signature": False})["subject"]
        return [*prev_chain, self.sign_hop(subject, prev_chain[-1])]


class InvalidChain(Exception):
    """An actor chain that fails verification: forged, reordered, truncated, spliced, expired, or malformed."""


@dataclass(frozen=True)
class VerifiedChain:
    """What a verified chain vouches for: its subject, and each hop's signing key in order."""

    subject: dict
    actor_public_keys: list[str]


def verify_chain(chain: list[str]) -> VerifiedChain:
    """Verifies an actor chain without souk: each hop's signature under its own embedded key, `prevHash` linkage, one subject throughout, expiry enforced on the last hop only.

    The independent twin of `souk.identity.verify_actor_chain` — same rules,
    pinned to each other by interop tests. Unlike souk's, it does not resolve
    keys to registered agent names (that needs souk's roster); it is the tool
    an LLM provider uses to police a delegation chain itself, trusting no
    summary of souk's. Raises `InvalidChain` on any failure.
    """
    if not chain:
        raise InvalidChain("empty actor chain")

    subject: dict | None = None
    actor_public_keys: list[str] = []
    prev_token: str | None = None

    for i, token in enumerate(chain):
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError as e:
            raise InvalidChain(f"hop {i}: unparseable token: {e}") from e

        actor_public_key = unverified.get("actorPublicKey")
        if not isinstance(actor_public_key, str):
            raise InvalidChain(f"hop {i}: missing actorPublicKey")
        try:
            public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(actor_public_key))
        except ValueError as e:
            raise InvalidChain(f"hop {i}: malformed actorPublicKey: {e}") from e

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
            raise InvalidChain(f"hop {i}: {reason}: {e}") from e

        expected_prev_hash = (
            hashlib.sha256(prev_token.encode()).hexdigest() if prev_token is not None else None
        )
        if payload.get("prevHash") != expected_prev_hash:
            raise InvalidChain(f"hop {i}: prevHash doesn't match — chain reordered, truncated, or spliced")

        if i == 0:
            subject = payload.get("subject")
            if not isinstance(subject, dict):
                raise InvalidChain("hop 0: missing subject")
        elif payload.get("subject") != subject:
            raise InvalidChain(f"hop {i}: subject changed partway through the chain")

        actor_public_keys.append(actor_public_key)
        prev_token = token

    assert subject is not None
    return VerifiedChain(subject=subject, actor_public_keys=actor_public_keys)
