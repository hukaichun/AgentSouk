"""A provider's identity, and what it signs — the provider's own copy.

**This file must not import souk, and that is the whole point of it.**

A provider's identity is an Ed25519 keypair; souk issues no account. What it
signs — the exact bytes of a registration or a deletion — is an interop
surface between two codebases, and an interop surface only stays honest if
both sides state it independently. Import souk's builder here and the two can
never disagree, which sounds like safety and is the opposite: a change to the
payload moves both sides at once and every test stays green while every real
provider stops being able to register.

That is not hypothetical. souk added an operation prefix to its registration
payload while its test suite signed with souk's own builder; 219 tests passed
and no provider in the world could register any more. Nothing in the tree
noticed, because nothing in the tree held a second opinion.

souk's suite holds one now — it registers through this module — so a payload
change fails there, at merge time, instead of downstream at deploy time. The
duplication is deliberate and load-bearing. Do not 'clean it up' by importing
`souk.identity`; that deletes the check.

(The same device already guards the actor-chain hop format, where souk's
conftest reimplements the SDK's signing rather than calling it.)
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import jwt
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

# How long one freshly-signed delegation hop stays usable. Only the last hop's
# expiry is enforced by souk, so this bounds "who is using this chain right
# now" rather than how long the provenance stays readable.
ACTOR_CHAIN_TTL_SECONDS = 300

# What a signed request is *for*. Without this, a registration for one agent
# and a deletion of that agent are the same bytes, so observing the first
# hands you the second — measured against souk before the prefixes existed.
_REGISTER = "souk-register"
_DELETE_AGENT = "souk-delete-agent"


def verify_signature(public_key_hex: str, signature_hex: str, payload: bytes) -> bool:
    """Did the holder of this public key sign these bytes?

    The half this package was missing. It could sign anything and check
    nothing, which is exactly the asymmetry souk had in reverse before
    `ProviderIdentity.sign` existed: souk published a general verifier while
    withholding a general signer.

    A provider needs this to check the *other* side. souk has an identity of
    its own now, and a handshake where only the provider proves itself is a
    handshake a provider cannot use to tell one souk from another. Verifying
    that is the point of souk having a key at all.

    Deliberately a second implementation rather than souk's, imported. This
    package does not depend on souk — that is the boundary — and something
    derived from souk agrees with souk by construction and therefore checks
    nothing. Same device as the signing payloads below: souk's own suite
    drives this pair against `souk.identity.verify_signature`, so an
    implementation that drifts fails at merge instead of at a handshake.

    False rather than raising, for every way this can fail — a bad signature,
    a malformed key, non-hex input. A caller is asking a yes/no question
    about data that arrived over a wire, and every no is the same answer.
    """
    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        public_key.verify(bytes.fromhex(signature_hex), payload)
        return True
    except (InvalidSignature, ValueError):
        return False


def registration_payload(agent_names: list[str], timestamp: int) -> bytes:
    """The names being claimed, sorted, and when. The identity is not in here
    and need not be: the signature is checked against the public key presented
    alongside it, so a captured payload cannot be re-presented under another
    key."""
    return f"{_REGISTER}:{','.join(sorted(agent_names))}:{timestamp}".encode()


def deletion_payload(agent_name: str, timestamp: int) -> bytes:
    """One name, never a batch: registering is declarative and idempotent,
    deleting is neither."""
    return f"{_DELETE_AGENT}:{agent_name}:{timestamp}".encode()


class ProviderIdentity:
    """The keypair a provider is.

    Persisted to disk, because restarting must not change who you are: an
    agent is `(provider_key, name)`, so a fresh key is a fresh, unrelated
    provider offering the same names, and anything pointing at the old pair
    keeps pointing at an identity nobody holds.
    """

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._private_key = private_key
        self.public_key = private_key.public_key().public_bytes_raw().hex()

    @classmethod
    def generate(cls) -> "ProviderIdentity":
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def load_or_create(cls, path: str | Path) -> "ProviderIdentity":
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
        """Sign arbitrary bytes with this identity.

        For payloads souk does not define. The three below cover the
        operations souk itself verifies; a transport proving who it is when it
        *opens a connection* is not one of them, because what a connection is
        belongs to whoever is serving souk — so the payload is theirs and this
        is the primitive they build it from.

        souk already publishes the verifying half generically
        (`souk.identity.verify_signature`, which takes arbitrary bytes), so
        this is the missing symmetric piece rather than a new capability:
        anything a gateway can check, the identity it is checking should be
        able to produce.

        No restriction on what may be signed, deliberately. Refusing souk's
        own payload prefixes was considered and dropped — it protects against
        nothing, since `sign_registration` and `sign_deletion` are public on
        this same object, and it would re-introduce a smaller version of
        exactly the problem this method exists to fix: this package deciding
        what its holder is allowed to sign. Keeping distinct operations
        distinct is the job of whoever defines the payload.
        """
        return self._private_key.sign(payload).hex()

    def sign_registration(self, agent_names: list[str], timestamp: int | None = None) -> tuple[str, int]:
        """Returns `(signature_hex, timestamp)` — both, because souk verifies
        the signature *over* the timestamp and would otherwise be handed two
        values that do not belong together."""
        timestamp = int(time.time()) if timestamp is None else timestamp
        return self._private_key.sign(registration_payload(agent_names, timestamp)).hex(), timestamp

    def sign_deletion(self, agent_name: str, timestamp: int | None = None) -> tuple[str, int]:
        timestamp = int(time.time()) if timestamp is None else timestamp
        return self._private_key.sign(deletion_payload(agent_name, timestamp)).hex(), timestamp

    # ---- Delegation provenance
    #
    # The third thing a provider signs, and one souk already treats as an
    # interop surface: an agent inside souk and one across a wire must be able
    # to appear in the same chain, so the hop format is stated on both sides
    # rather than shared.

    def sign_hop(
        self, subject: dict, prev_token: str | None = None, ttl: int = ACTOR_CHAIN_TTL_SECONDS
    ) -> str:
        """One hop of an actor chain.

        `subject` is who the chain is fundamentally about — this agent acting
        for itself, or a human it authenticated by means souk has no view of.
        souk never verifies that claim, because it cannot; what it verifies is
        that every actor signing a hop holds the key it claims, and that the
        chain was not altered since.
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
        return [self.sign_hop(subject)]

    def extend_chain(self, prev_chain: list[str]) -> list[str]:
        """Relay a chain onward, carrying its subject. Without this a hop is a
        dead end: the callee learns who is calling now, but not on whose
        behalf, with nothing tying the two together."""
        if not prev_chain:
            raise ValueError("extend_chain requires a non-empty chain — use new_chain to start one")
        subject = jwt.decode(prev_chain[-1], options={"verify_signature": False})["subject"]
        return [*prev_chain, self.sign_hop(subject, prev_chain[-1])]
