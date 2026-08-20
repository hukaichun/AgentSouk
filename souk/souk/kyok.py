"""Keep Your Own Key: run-scoped bearer tokens for souk's
OpenAI-compatible LLM bridge (see api_llm_bridge.py and
docs/keep-your-own-key.md for the full picture).

A KYOK token binds one run_id to the caller's bridge session_id (souk.
souk.kyok's KyokBridge) — the only thing /kyok/v1/chat/completions
needs from the "api_key" a provider sends it is "which session should
this be relayed to". Same signing mechanism as souk.identity.
the session token souk used to issue (HMAC over a base64 JSON body, same
token_signing_secret) — deliberately not that pair, since the payload shape
and what a forged one could do were different enough to be worth keeping
apart. That one is gone with the call it guarded; this is now the only thing
`token_signing_secret` signs.

Also carries the agent — souk already knows, at the moment it mints this
token (souk.api_agui._build_forwarded_props, called with the run's own
the pair), exactly which provider identity this run belongs to; the
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
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from souk.ids import new_id
from souk.models import AgentRef

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
    agent: AgentRef


def issue_kyok_token(run_id: str, session_id: str, agent: AgentRef, signing_secret: str) -> str:
    body = base64.urlsafe_b64encode(
        json.dumps(
            {
                "runId": run_id,
                "sessionId": session_id,
                "providerKey": agent.provider_key,
                "agentName": agent.name,
                "exp": int(time.time()) + KYOK_TOKEN_TTL_SECONDS,
            }
        ).encode()
    ).decode()
    signature = hmac.new(signing_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def verify_kyok_token(token: str, signing_secret: str) -> KyokToken | None:
    """Returns the decoded (run_id, session_id, agent) if `token` is a
    well-formed, correctly-signed, unexpired KYOK token, else None. Called
    on every /kyok/v1/chat/completions request — see api_llm_bridge.py,
    which additionally checks the returned agent against souk.broker's
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
    provider_key = payload.get("providerKey")
    agent_name = payload.get("agentName")
    if not all(
        isinstance(v, str) for v in (run_id, session_id, provider_key, agent_name)
    ):
        return None
    return KyokToken(
        run_id=run_id,
        session_id=session_id,
        agent=AgentRef(provider_key=provider_key, name=agent_name),
    )


# Sentinel put on a completion's response_queue once its bridge finishes
# sending chunks (or the /kyok/respond connection drops) — mirrors
# broker.END_OF_STREAM.
COMPLETION_DONE = object()


@dataclass
class _Session:
    """One caller's rendezvous point: what is queued for it, and who is
    waiting on it.

    A class rather than two entries in two maps, because the two have one
    lifetime and keeping them in step was a convention no type enforced. It
    also gives that lifetime somewhere to live: `is_idle` is the whole rule,
    in one place, instead of an invariant restated in prose over two dicts
    and three methods.
    """

    queue: deque[str] = field(default_factory=deque)
    waiters: set[asyncio.Event] = field(default_factory=set)

    def is_idle(self) -> bool:
        """Nothing queued and nobody waiting — so nothing could find this,
        and it has no reason to exist."""
        return not self.queue and not self.waiters


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

    Two maps, keyed by two very different kinds of string, and the difference
    is the whole design of this class:

    - `_requests` is keyed by a **souk-minted** `request_id` (`new_id`). Its
      keys cannot be influenced from outside, and it has a removal path
      (`forget`, called by protocols.kyok once a completion is done).
    - `_sessions` is keyed by a `session_id` souk neither mints nor
      authenticates. It is a rendezvous label the caller chooses, and
      `GET /kyok/poll` accepts any string by design (see
      docs/keep-your-own-key.md). The cardinality of *that* key space is
      under a stranger's control.

    A bare mapping records neither who may create a key nor when it stops
    existing, so the second kind needs its lifetime made explicit somewhere
    the type system can see it. It didn't have one. Both halves of a session
    lived in their own `defaultdict`, so a *lookup* inserted: one poll of an
    unseen session left an empty deque and, on the waiting path, an empty
    set, and nothing ever reclaimed either. 100k polls of distinct unknown
    sessions retained 81 MiB. Nor was it only adversarial — an ordinary
    finished session leaked its key too, forever, because `popleft()` empties
    a deque without removing it.

    So a session is now one `_Session` object with one lifetime rule
    (`is_idle`), created in exactly two places and dropped in exactly one:

        an entry exists exactly while there is something to find — a queued
        request (from `submit`, reachable only after protocols.kyok's
        three-part authorization) or a poll in flight (created and removed by
        that poll).

    What this still cannot give you is *provenance*: core does not know who
    opened a session, because that endpoint is deliberately unauthenticated.
    No container choice fixes that — the answer is upstream of here, and it
    is what moving the relay behind a hello-authenticated connection buys.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}
        self._requests: dict[str, PendingCompletion] = {}

    def submit(self, session_id: str, body: dict[str, Any]) -> tuple[str, asyncio.Queue[Any]]:
        """The only thing that creates a session entry — and the caller
        reaching it has already passed the token/live-run/signature checks in
        protocols.kyok's `complete`."""
        request_id = new_id("kyokreq")
        pending = PendingCompletion(session_id=session_id, body=body)
        self._requests[request_id] = pending
        session = self._sessions.setdefault(session_id, _Session())
        session.queue.append(request_id)
        for event in session.waiters:
            event.set()
        return request_id, pending.response_queue

    async def poll_one(self, session_id: str, wait_seconds: float) -> dict[str, Any] | None:
        """Returns `{"requestId": ..., "body": ...}` for the next
        completion queued for `session_id`, waiting up to `wait_seconds`
        if none is queued yet (0 means return immediately either way).
        None if nothing showed up within the wait.

        A read, and it behaves like one: an unknown session_id is answered
        without being recorded.
        """
        session = self._sessions.get(session_id)
        if (session is None or not session.queue) and wait_seconds > 0:
            await self._wait_for_work(session_id, wait_seconds)
        return self._take(session_id)

    async def _wait_for_work(self, session_id: str, wait_seconds: float) -> None:
        """Park until `submit` wakes this session or the wait runs out. The
        waiting *is* the session's reason to exist for that long, which is why
        this may create one — and why it must drop it again on the way out."""
        session = self._sessions.setdefault(session_id, _Session())
        event = asyncio.Event()
        # One `_Session` shared by every waiter, so the last one out is the
        # one that removes it: a concurrent waiter's event is still in this
        # same object, so `is_idle` is False while anyone else is parked.
        session.waiters.add(event)
        try:
            await asyncio.wait_for(event.wait(), timeout=wait_seconds)
        except asyncio.TimeoutError:
            pass
        finally:
            session.waiters.discard(event)
            self._drop_if_idle(session_id, session)

    def _take(self, session_id: str) -> dict[str, Any] | None:
        """The queue's head, dropping ids whose request is already gone.

        Skipping rather than giving up on the first dead id: a request
        abandoned before anyone polled it (see `forget`) would otherwise hide
        every live request queued behind it, and answer "nothing to do" to a
        bridge that has work waiting.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return None
        try:
            while session.queue:
                request_id = session.queue.popleft()
                pending = self._requests.get(request_id)
                if pending is None:
                    continue
                pending.claimed = True
                return {"requestId": request_id, "body": pending.body}
            return None
        finally:
            self._drop_if_idle(session_id, session)

    def _drop_if_idle(self, session_id: str, session: _Session) -> None:
        """The one place a session key is removed.

        Identity-checked before deleting: only ever drop the object this
        caller was actually working with, never whatever happens to be under
        that key now. Nothing here awaits, so today that cannot differ — the
        check is what keeps it true if anything above this ever does.
        """
        if session.is_idle() and self._sessions.get(session_id) is session:
            del self._sessions[session_id]

    def get(self, request_id: str) -> PendingCompletion | None:
        return self._requests.get(request_id)

    def forget(self, request_id: str) -> None:
        self._requests.pop(request_id, None)
