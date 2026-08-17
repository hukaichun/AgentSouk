"""The bridge loop on its own, against a link that is not souk.

Deliberately no souk here: this package's whole claim is that the loop does
not need one. The end-to-end pairing with a real souk lives in souk's own
suite (`souk/tests/test_kyok_in_process.py`), which is the other half of the
same argument.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from souk_caller_sdk import (
    CallerLink,
    KyokBridge,
    PendingCompletion,
    new_session_id,
    run_metadata,
)


class FakeLink(CallerLink):
    """Hands out a fixed list of completions, then nothing, and records every
    answer in full."""

    def __init__(self, queued: list[PendingCompletion]) -> None:
        self._queued = list(queued)
        self.answers: dict[str, list[dict]] = {}
        self.idle_claims = 0

    async def claim(self, session_id: str, wait_seconds: float) -> PendingCompletion | None:
        self.session_id = session_id
        if self._queued:
            return self._queued.pop(0)
        self.idle_claims += 1
        await asyncio.sleep(0)
        return None

    async def answer(self, request_id: str, chunks: AsyncIterator[dict[str, Any]]) -> None:
        self.answers[request_id] = [chunk async for chunk in chunks]


def _chunk(content: str) -> dict:
    return {"choices": [{"index": 0, "delta": {"content": content}}]}


def _source(*contents: str):
    async def complete(body: dict) -> AsyncIterator[dict]:
        for content in contents:
            yield _chunk(content)

    return complete


async def test_one_completion_reaches_the_callers_own_code_and_the_answer_goes_back():
    link = FakeLink([PendingCompletion(request_id="r1", body={"messages": ["hi"]})])
    seen = {}

    async def complete(body: dict) -> AsyncIterator[dict]:
        seen["body"] = body
        yield _chunk("hello")

    bridge = KyokBridge("sess", complete)
    await bridge.serve(link, PendingCompletion("r1", {"messages": ["hi"]}))

    assert seen["body"] == {"messages": ["hi"]}
    assert link.answers["r1"] == [_chunk("hello")]


async def test_a_failing_completion_source_becomes_an_error_chunk():
    """The provider is blocked on this answer. Telling it now beats letting
    souk's relay time out — which would also blame the bridge for not having
    claimed, when it had."""
    link = FakeLink([])

    async def explodes(body: dict) -> AsyncIterator[dict]:
        raise RuntimeError("no credit left")
        yield  # pragma: no cover - generator

    await KyokBridge("sess", explodes).serve(link, PendingCompletion("r2", {}))

    assert link.answers["r2"] == [{"error": "no credit left"}]


async def test_chunks_already_streamed_survive_a_later_failure():
    """A source that dies half way has still produced real output, and the
    provider should get what there was plus the error, not one or the other."""
    link = FakeLink([])

    async def half(body: dict) -> AsyncIterator[dict]:
        yield _chunk("par")
        raise RuntimeError("dropped")

    await KyokBridge("sess", half).serve(link, PendingCompletion("r3", {}))

    assert link.answers["r3"] == [_chunk("par"), {"error": "dropped"}]


async def test_serve_forever_keeps_claiming_and_serves_concurrently():
    """A slow completion must not hold up the next one: the loop goes back to
    claiming immediately, so both are in flight together."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def first_blocks(body: dict) -> AsyncIterator[dict]:
        if body["n"] == 1:
            started.set()
            await release.wait()
        yield _chunk(str(body["n"]))

    link = FakeLink(
        [PendingCompletion("r1", {"n": 1}), PendingCompletion("r2", {"n": 2})]
    )
    loop = asyncio.create_task(KyokBridge("sess", first_blocks).serve_forever(link))
    try:
        await asyncio.wait_for(started.wait(), 1)
        # r1 is parked inside the source; r2 must still get through.
        await asyncio.wait_for(_until(lambda: "r2" in link.answers), 1)
        release.set()
        await asyncio.wait_for(_until(lambda: "r1" in link.answers), 1)
    finally:
        loop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop

    assert link.answers["r1"] == [_chunk("1")]
    assert link.answers["r2"] == [_chunk("2")]


async def test_cancelling_the_loop_stops_paying_for_answers_in_flight():
    """An answer with nowhere to go is money spent for nothing, and this
    bridge is the thing paying."""
    cancelled = asyncio.Event()

    async def never_finishes(body: dict) -> AsyncIterator[dict]:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        yield {}  # pragma: no cover

    link = FakeLink([PendingCompletion("r1", {})])
    loop = asyncio.create_task(KyokBridge("sess", never_finishes).serve_forever(link))
    await asyncio.wait_for(_until(lambda: link.idle_claims > 0), 1)

    loop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop

    await asyncio.wait_for(cancelled.wait(), 1)


async def test_an_empty_claim_is_not_an_error():
    """An idle bridge spends nearly all its life here."""
    link = FakeLink([])
    loop = asyncio.create_task(KyokBridge("sess", _source()).serve_forever(link))
    await asyncio.wait_for(_until(lambda: link.idle_claims >= 3), 1)
    loop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop


def test_a_session_id_is_minted_locally_and_is_not_guessable():
    assert new_session_id() != new_session_id()
    assert len(new_session_id()) == 32  # 128 bits, hex


def test_run_metadata_states_the_one_key_souk_reads():
    assert run_metadata("sess_1") == {"kyok": {"sessionId": "sess_1"}}


async def _until(predicate, interval: float = 0.005) -> None:
    while not predicate():
        await asyncio.sleep(interval)
