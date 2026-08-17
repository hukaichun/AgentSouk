"""The caller's side of Keep Your Own Key: claim a completion, call your own
LLM, stream the answer back. The loop, and nothing about how it travels.

A provider running an agent for you needs an LLM. KYOK's answer is that it
calls souk, souk queues the request for whoever is paying, and that party —
you — makes the real call with your own key. This is the "you" half. Which
model, which vendor, which key, and whether to refuse at all are yours: they
arrive as a `CompletionSource` you supply, because a package that chose an LLM
client for you would be making the single decision KYOK exists to leave you.

The transport is a `CallerLink`. In this repo the only one is
`InProcessLink`, which is what lets the whole loop be a test; a socket is one
downstream (see the gateway's `WS /ws/kyok`).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator, Callable
from typing import Any

from souk_caller_sdk.contract import ERROR_CHUNK_KEY, RUN_METADATA_KEY
from souk_caller_sdk.link import CallerLink, PendingCompletion

logger = logging.getLogger("souk_caller_sdk.bridge")

# One cycle of the claim loop. A bridge spends nearly all its life here with
# nothing to do, so this is a wait, not a poll interval — there is no sleep
# between cycles and no cost to coming back empty.
CLAIM_WAIT_SECONDS = 25.0

# One OpenAI-shaped request body in, OpenAI-shaped streaming chunks out.
CompletionSource = Callable[[dict[str, Any]], AsyncIterator[dict[str, Any]]]


def new_session_id() -> str:
    """A bridge session id, minted locally.

    souk never hands one out — it accepts whichever shows up and whichever
    run's metadata names it, so nothing has to be reserved before the run
    exists. 128 bits because knowing it is, today, the entire proof that a
    connection is this caller's bridge; see the "Opening a bridge session"
    section of souk's docs/keep-your-own-key.md for what that does and does
    not buy, and for what it would take to make it a real credential.
    """
    return secrets.token_hex(16)


def run_metadata(session_id: str) -> dict[str, Any]:
    """What to pass as a run's `metadata` to offer KYOK for that run.

    Here rather than left to the caller to spell out, so the one string souk
    reads has one definition. A caller that never calls this is not offering
    KYOK, and the provider simply never sees a token.
    """
    return {RUN_METADATA_KEY: {"sessionId": session_id}}


class KyokBridge:
    """One caller's bridge: serves every completion queued for its session.

    Not `souk.kyok.KyokBridge`, which is souk's own registry of what is queued
    for whom. Same name from two vantage points, and they meet only in
    `InProcessLink` and its tests.

    Holds no durable state. If this process dies mid-answer the completions it
    was serving fail, and the provider sees errors — there is no resume path
    on either side, which is one of the reasons KYOK is still experimental.
    """

    def __init__(
        self,
        session_id: str,
        complete: CompletionSource,
        *,
        claim_wait_seconds: float = CLAIM_WAIT_SECONDS,
    ) -> None:
        self.session_id = session_id
        self._complete = complete
        self._claim_wait_seconds = claim_wait_seconds

    async def serve_forever(self, link: CallerLink) -> None:
        """Claim and serve until cancelled.

        Each completion is served on its own task and the loop goes straight
        back to claiming, so a slow model does not hold up the next request —
        concurrent completions just interleave. Intended to run alongside the
        run it is serving, not to be awaited to completion: a bridge has no
        natural end of its own, the run does.
        """
        in_flight: set[asyncio.Task] = set()
        try:
            while True:
                pending = await link.claim(self.session_id, self._claim_wait_seconds)
                if pending is None:
                    continue
                task = asyncio.create_task(self.serve(link, pending))
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)
        finally:
            # An answer with nowhere to go is money spent for nothing: this
            # bridge is the thing paying, so it stops paying when it stops
            # being able to deliver.
            for task in in_flight:
                task.cancel()

    async def serve(self, link: CallerLink, pending: PendingCompletion) -> None:
        """One completion, start to finish.

        A failing completion source is reported as an error chunk rather than
        raised: the provider is blocked on this answer, and telling it now
        beats letting souk's relay time out — which would also blame the wrong
        side.
        """
        await link.answer(pending.request_id, self._chunks(pending))

    async def _chunks(self, pending: PendingCompletion) -> AsyncIterator[dict[str, Any]]:
        try:
            async for chunk in self._complete(pending.body):
                yield chunk
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("kyok bridge: completion %s failed", pending.request_id)
            yield {ERROR_CHUNK_KEY: str(e)}
