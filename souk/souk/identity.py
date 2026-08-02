"""Minimal provider identity: proves whoever registers an agent name
actually holds the private key that first claimed it, and gates gRPC
calls on a short-lived bearer token issued only after that proof.

Deliberately not a full account system — no signup flow, no stored
credentials beyond the public key an agent name was first claimed with.
A provider's identity *is* its Ed25519 keypair (see
souk_agent_sdk.identity, which generates and persists one on first
run). This is enough to close the one gap that matters for a public,
multi-tenant souk: nobody can register/reclaim an agent name they don't
hold the key for.

Two independent checks, at two different points:
  1. Registration (register_agents / verify_registration_signature):
     the request must be signed with the private key matching the
     public_key it presents, and that public_key must match whatever
     first claimed each agent name in the batch (see
     repo.register_agents). Expensive-ish (Ed25519 verify), so it only
     happens at registration, not on every poll.
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

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from souk.config import settings

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


def a2a_call_signing_payload(task_id: str, session_id: str | None, timestamp: int) -> bytes:
    """What a *caller* signs when it wants souk to know who it is on an
    A2A tasks/send(Subscribe) call — see api_a2a._start_run. Currently
    used for agent-to-agent calls (a provider signing with the same
    identity key it registered with, see
    providers/pydantic-ai-agent/pydantic_ai_agent/sub_agent_tool.py) but
    nothing here is specific to that — any caller holding an Ed25519 key
    could use the same mechanism. Doesn't cover the message text itself:
    the goal is knowing *who* initiated this task, not tamper-proofing
    its content.
    """
    return f"{task_id}:{session_id or ''}:{timestamp}".encode()


def verify_signature(public_key_hex: str, signature_hex: str, payload: bytes) -> bool:
    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        public_key.verify(bytes.fromhex(signature_hex), payload)
        return True
    except (InvalidSignature, ValueError):
        return False


def issue_session_token(sdk_client_id: str) -> str:
    body = base64.urlsafe_b64encode(
        json.dumps({"sdk_client_id": sdk_client_id, "exp": int(time.time()) + SESSION_TOKEN_TTL_SECONDS}).encode()
    ).decode()
    signature = hmac.new(settings.token_signing_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def verify_session_token(token: str) -> str | None:
    """Returns the token's sdk_client_id if valid (correct signature, not
    expired), else None. Called on every PollForWork/AgentSession — see
    souk.grpc_server.
    """
    try:
        body, signature = token.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(settings.token_signing_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
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
