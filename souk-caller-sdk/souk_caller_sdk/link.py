"""souk, as a caller's bridge reaches it: two methods, no transport.

The counterpart of `souk_provider_sdk.SoukLink`. A base class rather than a
`Protocol` for the same reason that one is: a carrier that forgets a method
fails at construction instead of at the first completion it cannot answer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any


@dataclass
class PendingCompletion:
    """One completion waiting to be served, in this package's own words.

    `body` is an OpenAI-shaped chat-completions request exactly as the
    provider sent it — souk relays it unread beyond parsing the JSON, so
    whatever the provider's model client produced is what arrives.
    """

    request_id: str
    body: dict[str, Any]


class CallerLink:
    """A souk this bridge can serve.

    Both methods take and return this package's own shapes; mapping them onto
    souk's is the carrier's job (see `inprocess.InProcessLink` for the shortest
    one there is).
    """

    async def claim(self, session_id: str, wait_seconds: float) -> PendingCompletion | None:
        """The next completion queued for `session_id`, waiting up to
        `wait_seconds` for one to appear. None if none did — an ordinary
        answer, not an error: an idle bridge spends most of its life here.
        """
        raise NotImplementedError

    async def answer(self, request_id: str, chunks: AsyncIterator[dict[str, Any]]) -> None:
        """Stream one completion's answer back, chunk by chunk.

        Consumed incrementally on both sides, so a long answer never sits
        whole in memory anywhere. Ending the iterator ends the answer; a chunk
        carrying `error` fails the completion instead (see
        `contract.ERROR_CHUNK_KEY`).
        """
        raise NotImplementedError
