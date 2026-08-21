from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

import jwt
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ACTOR_CHAIN_TTL_SECONDS = 300

_REGISTER = "funduq-register"
_DELETE_AGENT = "funduq-delete-agent"
_KYOK_CALL = "funduq-kyok-call"


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

    Both rosters sign these bytes — agents under `funduq-register`, LLM
    offerings under `funduq-register-llm` — and the tag is what keeps a
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


_CONNECT_PROVIDER = "funduq-connect-provider"
_CONNECT_FUNDUQ = "funduq-connect-funduq"


def new_nonce() -> str:
    """A fresh 128-bit hex nonce for one side of a link-open challenge."""
    return secrets.token_hex(16)


def provider_connect_payload(
    funduq_public_key: str, funduq_nonce: str, provider_nonce: str, names: list[str]
) -> bytes:
    """Builds the bytes a provider signs to authenticate opening a link.

    `funduq_nonce` is the challenge the funduq being connected to chose — that is
    what makes a recorded exchange worthless — and the names to be served are
    bound in (sorted) so they cannot be altered in flight.
    `funduq_public_key` is the recipient: the funduq key you pinned (empty
    string for a funduq with no identity). It is what stops a funduq you
    connect to from relaying your proof to attach elsewhere as you — the
    verifying funduq builds this payload with its own key, so a proof bound
    to the wrong funduq fails there. Do not substitute a timestamp for the
    nonce: a self-chosen freshness is replayable for its whole window,
    which is the hole this family exists to close.
    `funduq.identity.provider_connect_signing_payload` computes this same
    payload independently on funduq's side and both must agree byte-for-byte.
    """
    return (
        f"{_CONNECT_PROVIDER}:{funduq_public_key}:{funduq_nonce}:{provider_nonce}:"
        f"{','.join(sorted(names))}".encode()
    )


class WrongFunduq(Exception):
    """The funduq answering a link-open did not prove the key you pinned.

    Raised before any run or event has crossed — the point of the pin is
    refusing to produce anything worth stealing for an imposter."""


def delegation_payload(delegate_public_key: str, expires_at: int) -> bytes:
    """The session delegation certificate's signed bytes: a durable key names an ephemeral
    delegate key and an expiry. The independent twin of
    `funduq.identity.delegation_signing_payload`."""
    return f"funduq-delegate:{delegate_public_key}:{expires_at}".encode()


def resolve_payload(run_id: str, timestamp: int) -> bytes:
    """The bytes an authority signs to answer a paused (input-required) run. The independent
    twin of `funduq.identity.resolve_signing_payload`; `timestamp` is checked against funduq's
    60s freshness window."""
    return f"funduq-resolve:{run_id}:{timestamp}".encode()


def funduq_connect_payload(funduq_nonce: str, provider_nonce: str) -> bytes:
    """Builds the bytes funduq signs to prove itself to a connecting provider.

    Verify it with `verify_signature` against the funduq public key you
    pinned, before sending anything worth stealing; `provider_nonce` is
    yours, so the proof cannot be a recording. The role tag differs from
    `provider_connect_payload`'s so neither proof can be reflected as the
    other. `funduq.identity.funduq_connect_signing_payload` is the independent
    twin.
    """
    return f"{_CONNECT_FUNDUQ}:{funduq_nonce}:{provider_nonce}".encode()


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

    def sign_connect(
        self, funduq_public_key: str, funduq_nonce: str, provider_nonce: str, names: list[str]
    ) -> str:
        """Signs `provider_connect_payload(...)`: this provider's answer to a funduq's link-open challenge, bound to the funduq key it means to connect to."""
        return self.sign(
            provider_connect_payload(funduq_public_key, funduq_nonce, provider_nonce, names)
        )


    def sign_delegation(self, delegate_public_key: str, ttl_seconds: int = 8 * 3600) -> dict:
        """Issues a session delegation certificate: this identity (the durable authority)
        names `delegate_public_key` to act for it until now+`ttl_seconds`. Returns the wire
        form `{authorityPublicKey, delegatePublicKey, expiresAt, signature}` that
        `funduq.identity.verify_delegation` accepts. The certificate alone moves nothing
        without the delegate's private key, so it is safe to store and relay."""
        expires_at = int(time.time()) + ttl_seconds
        return {
            "authorityPublicKey": self.public_key,
            "delegatePublicKey": delegate_public_key,
            "expiresAt": expires_at,
            "signature": self.sign(delegation_payload(delegate_public_key, expires_at)),
        }

    def sign_resolution(self, run_id: str, timestamp: int | None = None) -> tuple[str, int]:
        """Signs the resolution of a paused run: `(signature, timestamp)` over
        `resolve_payload(run_id, timestamp)`. Singular operation, timestamp family — funduq
        checks the timestamp against its 60s freshness window and the status-guarded reopen
        consumes the signature with the win."""
        timestamp = int(time.time()) if timestamp is None else timestamp
        return self.sign(resolve_payload(run_id, timestamp)), timestamp

    def sign_hop(
        self, prev_token: str | None = None, ttl: int = ACTOR_CHAIN_TTL_SECONDS
    ) -> str:
        """Issues a JWT (EdDSA) hop under this identity's public key, optionally chained to `prev_token` via its sha256 in `prevHash`, expiring after `ttl` seconds.

        A chain carries keys and nothing else — the first hop's signer is
        the segment's head/authority; whom a key represents is a separate,
        opt-in disclosure (a voucher), never a hop field.
        `funduq.identity.verify_actor_chain` is the verifier these hops must
        satisfy; it builds the same claim format independently, and any
        change here must stay verifiable by it.
        """
        now = int(time.time())
        return jwt.encode(
            {
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

    def new_chain(self) -> list[str]:
        """Starts a new actor chain: a one-element list holding a single signed hop — this identity is the chain's head."""
        return [self.sign_hop()]

    def extend_chain(self, prev_chain: list[str]) -> list[str]:
        """Appends a hop signed by this identity. Extension is provenance, not authorization: the head stays the segment's authority.

        Raises ValueError if `prev_chain` is empty — use `new_chain` to start one.
        """
        if not prev_chain:
            raise ValueError("extend_chain requires a non-empty chain — use new_chain to start one")
        return [*prev_chain, self.sign_hop(prev_chain[-1])]


class InvalidChain(Exception):
    """An actor chain that fails verification: forged, reordered, truncated, spliced, expired, or malformed."""


@dataclass(frozen=True)
class VerifiedChain:
    """A verified chain: each hop's signing key in order, head first."""

    actor_public_keys: list[str]

    @property
    def head(self) -> str:
        """The first hop's signer: the responsibility segment's authority."""
        return self.actor_public_keys[0]


def verify_chain(chain: list[str]) -> VerifiedChain:
    """Verifies an actor chain without funduq: each hop's signature under its own embedded key, `prevHash` linkage, expiry enforced on the last hop only.

    The independent twin of `funduq.identity.verify_actor_chain` — same rules,
    pinned to each other by interop tests. Unlike funduq's, it does not resolve
    keys to registered agent names (that needs funduq's roster); it is the tool
    an LLM provider uses to police a delegation chain itself, trusting no
    summary of funduq's. Raises `InvalidChain` on any failure.
    """
    if not chain:
        raise InvalidChain("empty actor chain")

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

        actor_public_keys.append(actor_public_key)
        prev_token = token

    return VerifiedChain(actor_public_keys=actor_public_keys)
