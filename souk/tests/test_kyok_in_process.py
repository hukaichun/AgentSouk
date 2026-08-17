"""The whole Keep Your Own Key loop, in one process.

A provider asks for a completion, souk relays it to whoever is paying, that
party's own code answers, and the chunks arrive back at the provider. Every
KYOK test written before this one — in either repo — stopped at a frame: they
either drove souk's adapter with hand-made queue entries, or drove a socket
with hand-made JSON. Nothing joined the two ends, so nothing could tell you
whether the thing actually works.

It needs no gateway, no socket and no key, which is the point of
`souk_caller_sdk.InProcessLink` existing. In-process is a transport here, not
a shortcut: the completion goes through the same `KyokAdapter.complete` a
provider's HTTP call lands on, with a real Ed25519 signature over a real body,
and the bridge is reached by the same session routing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest

from souk_caller_sdk import InProcessLink, KyokBridge, new_session_id, run_metadata

from souk.errors import KyokRejected
from souk.kyok import issue_kyok_token
from souk.models import AgentRef
from souk.protocols.kyok import KyokAdapter

SIGNING_SECRET = "test-signing-secret"


def _signed(bearer: str, identity, body: bytes) -> dict[str, Any]:
    """What a provider's `KyokSigningAuth` puts on the wire, reconstructed —
    the payload written out here rather than imported, for the same reason
    `souk_provider_sdk.identity` writes it out: a copy derived from souk agrees
    with souk by construction and checks nothing."""
    timestamp = int(time.time())
    payload = f"souk-kyok-call:{bearer}:{timestamp}:{hashlib.sha256(body).hexdigest()}".encode()
    return {"timestamp": str(timestamp), "signature": identity.sign(payload)}


async def _live_run(souk, register, session_id: str, run_id: str = "run_kyok_ip"):
    """A registered agent, a run the broker is dispatching, and a token naming
    both — what `_build_forwarded_props` would have handed the provider."""
    served = await register("translator")
    agent = AgentRef(provider_key=served.identity.public_key, name="translator")
    souk.enqueue_run(run_id, agent, "thread_1", run_metadata(session_id), "ag-ui")
    return served, issue_kyok_token(run_id, session_id, agent, SIGNING_SECRET)


async def _relay_completion(souk, served, token, body: dict) -> Any:
    raw = json.dumps(body).encode()
    headers = _signed(token, served.identity, raw)
    return await KyokAdapter(souk).complete(
        token, raw, timestamp=headers["timestamp"], signature=headers["signature"]
    )


async def _with_bridge(souk, bridge: KyokBridge, coro):
    """Run `coro` with the bridge serving alongside it, as a caller would."""
    serving = asyncio.create_task(bridge.serve_forever(InProcessLink(KyokAdapter(souk))))
    try:
        return await asyncio.wait_for(coro, timeout=5)
    finally:
        serving.cancel()
        try:
            await serving
        except asyncio.CancelledError:
            pass


async def test_a_provider_gets_the_answer_the_caller_paid_for(souk, register):
    session_id = new_session_id()
    served, token = await _live_run(souk, register, session_id)
    asked: dict = {}

    async def my_own_llm(body: dict) -> AsyncIterator[dict]:
        asked["body"] = body
        for word in ("bon", "jour"):
            yield {
                "id": "c1",
                "object": "chat.completion.chunk",
                "model": "whatever-i-pay-for",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": word}}],
            }
        yield {"id": "c1", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}

    try:

        async def provider_call():
            relay = await _relay_completion(
                souk, served, token, {"messages": [{"role": "user", "content": "hello"}]}
            )
            return await relay.collapsed()

        answer = await _with_bridge(souk, KyokBridge(session_id, my_own_llm), provider_call())
    finally:
        souk.broker.forget("run_kyok_ip")

    # The caller's code saw the provider's actual prompt...
    assert asked["body"]["messages"] == [{"role": "user", "content": "hello"}]
    # ...and the provider got back what the caller's model produced.
    assert answer["choices"][0]["message"] == {"role": "assistant", "content": "bonjour"}
    assert answer["choices"][0]["finish_reason"] == "stop"
    assert answer["model"] == "whatever-i-pay-for"


async def test_a_caller_that_refuses_fails_the_provider_rather_than_stalling_it(
    souk, register
):
    """The caller's own code is the last word on whether money moves — a
    refusal has to reach the provider as a failure, not as a timeout blamed on
    a bridge that was right there."""
    session_id = new_session_id()
    served, token = await _live_run(souk, register, session_id, run_id="run_kyok_refuse")

    async def i_decline(body: dict) -> AsyncIterator[dict]:
        raise RuntimeError("model not on my allow-list")
        yield  # pragma: no cover - generator

    try:

        async def provider_call():
            relay = await _relay_completion(souk, served, token, {"messages": []})
            return [chunk async for chunk in relay.chunks]

        chunks = await _with_bridge(souk, KyokBridge(session_id, i_decline), provider_call())
    finally:
        souk.broker.forget("run_kyok_refuse")

    assert chunks == [{"error": "model not on my allow-list"}]


async def test_a_bridge_on_another_session_is_never_given_this_work(souk, register):
    """The routing that the whole session-hash fix protects, exercised rather
    than asserted: a bridge is served its own session and nobody else's."""
    session_id = new_session_id()
    served, token = await _live_run(souk, register, session_id, run_id="run_kyok_other")
    stranger_saw: list[dict] = []

    async def mine(body: dict) -> AsyncIterator[dict]:
        yield {"choices": [{"index": 0, "delta": {"content": "mine"}, "finish_reason": "stop"}]}

    async def theirs(body: dict) -> AsyncIterator[dict]:
        stranger_saw.append(body)
        yield {"choices": [{"index": 0, "delta": {"content": "stolen"}}]}

    stranger = asyncio.create_task(
        KyokBridge(new_session_id(), theirs).serve_forever(InProcessLink(KyokAdapter(souk)))
    )
    try:

        async def provider_call():
            relay = await _relay_completion(souk, served, token, {"messages": ["secret"]})
            return await relay.collapsed()

        answer = await _with_bridge(souk, KyokBridge(session_id, mine), provider_call())
    finally:
        stranger.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stranger
        souk.broker.forget("run_kyok_other")

    assert stranger_saw == []
    assert answer["choices"][0]["message"]["content"] == "mine"


async def test_the_run_must_still_be_live(souk, register):
    """A token outlives its run by up to an hour; the run does not. Checked
    here through the same call a provider makes, not against the token alone."""
    session_id = new_session_id()
    served, token = await _live_run(souk, register, session_id, run_id="run_kyok_dead")
    souk.broker.forget("run_kyok_dead")

    with pytest.raises(KyokRejected) as excinfo:
        await _relay_completion(souk, served, token, {"messages": []})

    assert excinfo.value.status == 403


async def test_a_signature_from_another_provider_is_refused(souk, register):
    """The token says which agent this run belongs to; the signature has to
    come from that agent's key, not merely from *a* registered one."""
    session_id = new_session_id()
    served, token = await _live_run(souk, register, session_id, run_id="run_kyok_wrongkey")
    someone_else = await register("impostor")
    try:
        raw = json.dumps({"messages": []}).encode()
        headers = _signed(token, someone_else.identity, raw)

        with pytest.raises(KyokRejected) as excinfo:
            await KyokAdapter(souk).complete(
                token, raw, timestamp=headers["timestamp"], signature=headers["signature"]
            )
    finally:
        souk.broker.forget("run_kyok_wrongkey")

    assert excinfo.value.status == 401
