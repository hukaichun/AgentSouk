"""souk in this process: the shortest `CallerLink` there is.

Deliberately named like the others — `InProcessLink` here, as in
`souk_provider_sdk` — because in-process is a transport, not a special case.
Nothing it gets is a shortcut a remote bridge does not get: the same session
routing, and the same handshake if the bridge side ever gains a credential.
A convenience only in-process enjoys is the next bug.

This is the one module here that names souk's shapes, and it still does not
import souk. `souk` is anything with

    async poll(session_id, wait_seconds) -> {"requestId": str, "body": dict} | None
    async respond(request_id, chunks) -> None

which `souk.protocols.kyok.KyokAdapter` satisfies. Souk's own suite asserts
that it still does (`souk/tests/test_caller_sdk_contract.py`), so a rename on
either side fails at merge rather than at a customer.

What this buys, and the reason it exists at all: the whole KYOK loop — a
provider asking for a completion, the caller's own code answering it, the
chunks arriving back — becomes a test with no gateway, no socket and no key.
That loop had never been exercised end to end in any repo; every test on both
sides stopped at a frame.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from souk_caller_sdk.link import CallerLink, PendingCompletion


class InProcessLink(CallerLink):
    """A `KyokBridge` and a souk in one process, joined.

    A function call each way. Neither direction needs a queue, a frame or a
    correlation id — that is what a transport is for, and there is no
    transport.
    """

    def __init__(self, souk: Any) -> None:
        self._souk = souk

    async def claim(self, session_id: str, wait_seconds: float) -> PendingCompletion | None:
        queued = await self._souk.poll(session_id, wait_seconds)
        if queued is None:
            return None
        # The one place souk's names are spoken. `requestId`/`body` is souk's
        # spelling; `request_id`/`body` is this package's.
        return PendingCompletion(request_id=queued["requestId"], body=queued["body"])

    async def answer(self, request_id: str, chunks: AsyncIterator[dict[str, Any]]) -> None:
        await self._souk.respond(request_id, chunks)
