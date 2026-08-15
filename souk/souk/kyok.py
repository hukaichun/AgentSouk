"""Keep Your Own Key: run-scoped bearer tokens for souk's
OpenAI-compatible LLM bridge (see api_llm_bridge.py and
docs/keep-your-own-key.md for the full picture).

A KYOK token binds one run_id to the caller's bridge session_id (souk.
souk.kyok's KyokBridge) — the only thing /kyok/v1/chat/completions
needs from the "api_key" a provider sends it is "which session should
this be relayed to". Same signing mechanism as souk.identity.
issue_session_token/verify_session_token (HMAC over a base64 JSON body,
same token_signing_secret) — deliberately not reusing that pair directly
since the payload shape (and what a forged one could be used for —
routing a completion, not authenticating a worker call) is different enough
to be worth keeping separate.

Also carries `agent_id` — souk already knows, at the moment it mints this
token (souk.api_agui._build_forwarded_props, called with the run's own
agent_id), exactly which provider identity this run belongs to; the
token says so explicitly rather than leaving that implicit. See
protocols.kyok's KyokAdapter.complete for where this gets checked against
souk.broker's live view of who's actually running run_id right now — a
provider's identity is real (its Ed25519 keypair, souk_agent_sdk.
identity) even though this HTTP endpoint itself, unlike a worker's own
calls, carries no bearer proving it on the wire (staying
OpenAI-wire-compatible rules that out) — this is how the binding still
happens without needing one.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from souk.ids import new_id

# Deliberately short: a KYOK token only needs to live from "souk minted
# it into this run's forwardedProps" to "the provider's last completion
# call for this run" — not the whole lifetime of a possibly-long-running
# run. Provider code that holds a run open longer than this for its LLM
# calls would need a longer TTL; not a case that's come up yet.
KYOK_TOKEN_TTL_SECONDS = 3600


@dataclass
class KyokToken:
    run_id: str
    session_id: str
    agent_id: str


def issue_kyok_token(run_id: str, session_id: str, agent_id: str, signing_secret: str) -> str:
    body = base64.urlsafe_b64encode(
        json.dumps(
            {
                "runId": run_id,
                "sessionId": session_id,
                "agentId": agent_id,
                "exp": int(time.time()) + KYOK_TOKEN_TTL_SECONDS,
            }
        ).encode()
    ).decode()
    signature = hmac.new(signing_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def verify_kyok_token(token: str, signing_secret: str) -> KyokToken | None:
    """Returns the decoded (run_id, session_id, agent_id) if `token` is a
    well-formed, correctly-signed, unexpired KYOK token, else None. Called
    on every /kyok/v1/chat/completions request — see api_llm_bridge.py,
    which additionally checks the returned agent_id against souk.broker's
    live record of who's running run_id right now; this function only
    checks the token is genuinely souk's own and hasn't expired.
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
    session_id = payload.get("sessionId")
    agent_id = payload.get("agentId")
    if not isinstance(run_id, str) or not isinstance(session_id, str) or not isinstance(agent_id, str):
        return None
    return KyokToken(run_id=run_id, session_id=session_id, agent_id=agent_id)


# Sentinel put on a completion's response_queue once its bridge finishes
# sending chunks (or the /kyok/respond connection drops) — mirrors
# broker.END_OF_STREAM.
COMPLETION_DONE = object()


@dataclass
class PendingCompletion:
    session_id: str
    body: dict[str, Any]
    response_queue: asyncio.Queue[Any] = field(default_factory=asyncio.Queue)
    claimed: bool = False


class KyokBridge:
    """In-memory registry connecting a provider's queued completion
    requests to whichever caller session they belong to — same
    single-process assumption as broker.RunBroker (see its module
    docstring), for the same reason: none of this needs to survive a
    restart.
    """

    def __init__(self) -> None:
        self._pending_by_session: dict[str, deque[str]] = defaultdict(deque)
        self._requests: dict[str, PendingCompletion] = {}
        self._wake_subscribers: dict[str, set[asyncio.Event]] = defaultdict(set)

    def submit(self, session_id: str, body: dict[str, Any]) -> tuple[str, asyncio.Queue[Any]]:
        request_id = new_id("kyokreq")
        pending = PendingCompletion(session_id=session_id, body=body)
        self._requests[request_id] = pending
        self._pending_by_session[session_id].append(request_id)
        for event in self._wake_subscribers.get(session_id, ()):
            event.set()
        return request_id, pending.response_queue

    async def poll_one(self, session_id: str, wait_seconds: float) -> dict[str, Any] | None:
        """Returns `{"requestId": ..., "body": ...}` for the next
        completion queued for `session_id`, waiting up to `wait_seconds`
        if none is queued yet (0 means return immediately either way).
        None if nothing showed up within the wait.
        """
        queue = self._pending_by_session[session_id]
        if not queue and wait_seconds > 0:
            event = asyncio.Event()
            self._wake_subscribers[session_id].add(event)
            try:
                await asyncio.wait_for(event.wait(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                pass
            finally:
                self._wake_subscribers[session_id].discard(event)
        if not queue:
            return None
        request_id = queue.popleft()
        pending = self._requests.get(request_id)
        if pending is None:
            return None
        pending.claimed = True
        return {"requestId": request_id, "body": pending.body}

    def get(self, request_id: str) -> PendingCompletion | None:
        return self._requests.get(request_id)

    def forget(self, request_id: str) -> None:
        self._requests.pop(request_id, None)
