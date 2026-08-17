from __future__ import annotations

import hashlib
import time
from pathlib import Path

import jwt
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ACTOR_CHAIN_TTL_SECONDS = 300

_REGISTER = "souk-register"
_DELETE_AGENT = "souk-delete-agent"
_KYOK_CALL = "souk-kyok-call"


def verify_signature(public_key_hex: str, signature_hex: str, payload: bytes) -> bool:
    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        public_key.verify(bytes.fromhex(signature_hex), payload)
        return True
    except (InvalidSignature, ValueError):
        return False


def registration_payload(agent_names: list[str], timestamp: int) -> bytes:
    return f"{_REGISTER}:{','.join(sorted(agent_names))}:{timestamp}".encode()


def deletion_payload(agent_name: str, timestamp: int) -> bytes:
    return f"{_DELETE_AGENT}:{agent_name}:{timestamp}".encode()


def kyok_call_payload(bearer: str, timestamp: int, body_hash: str) -> bytes:
    return f"{_KYOK_CALL}:{bearer}:{timestamp}:{body_hash}".encode()


class ProviderIdentity:

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
        return self._private_key.sign(payload).hex()

    def sign_registration(self, agent_names: list[str], timestamp: int | None = None) -> tuple[str, int]:
        timestamp = int(time.time()) if timestamp is None else timestamp
        return self._private_key.sign(registration_payload(agent_names, timestamp)).hex(), timestamp

    def sign_deletion(self, agent_name: str, timestamp: int | None = None) -> tuple[str, int]:
        timestamp = int(time.time()) if timestamp is None else timestamp
        return self._private_key.sign(deletion_payload(agent_name, timestamp)).hex(), timestamp


    def sign_hop(
        self, subject: dict, prev_token: str | None = None, ttl: int = ACTOR_CHAIN_TTL_SECONDS
    ) -> str:
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
        if not prev_chain:
            raise ValueError("extend_chain requires a non-empty chain — use new_chain to start one")
        subject = jwt.decode(prev_chain[-1], options={"verify_signature": False})["subject"]
        return [*prev_chain, self.sign_hop(subject, prev_chain[-1])]
