"""PollForWork's long-poll branch (souk_server.grpc_server) — a provider that
finds nothing queued and sets wait_seconds should get a response as soon as
a run is enqueued for it, not have to wait out the full timeout. Backs the
souk<->provider latency fix: idle providers long-poll instead of holding a
permanent AgentSession stream open (see souk_agent_sdk.client).
"""

from __future__ import annotations

import asyncio
import time

from souk import repo
from souk_server.grpc_gen import souk_pb2
from souk_server.grpc_server import SoukAgentGatewayServicer
from souk.identity import issue_session_token


class _FakeContext:
    def __init__(self, token: str) -> None:
        self._token = token

    def invocation_metadata(self):
        return (("authorization", self._token),)


async def test_poll_for_work_returns_as_soon_as_a_run_is_enqueued(session, souk, new_identity):
    identity = new_identity()
    agent_ids = await repo.register_agents(session, identity.public_key, [{"name": "greeter"}])
    await session.commit()
    agent_id = agent_ids["greeter"]

    servicer = SoukAgentGatewayServicer(souk)
    context = _FakeContext(issue_session_token(identity.public_key, "test-signing-secret"))
    request = souk_pb2.PollRequest(agent_ids=[agent_id], wait_seconds=10)

    async def enqueue_soon() -> None:
        await asyncio.sleep(0.05)
        souk.broker.enqueue_run("run_1", agent_id, "thread_1", {}, "ag-ui")

    enqueue_task = asyncio.create_task(enqueue_soon())
    start = time.monotonic()
    response = await servicer.PollForWork(request, context)
    elapsed = time.monotonic() - start
    await enqueue_task

    assert [p.run_id for p in response.pending] == ["run_1"]
    # Well under the 10s wait_seconds timeout — proves the wake woke the
    # call rather than it just sitting out the full wait.
    assert elapsed < 2


async def test_poll_for_work_returns_empty_after_wait_seconds_with_no_work(session, souk, new_identity):
    identity = new_identity()
    agent_ids = await repo.register_agents(session, identity.public_key, [{"name": "greeter"}])
    await session.commit()
    agent_id = agent_ids["greeter"]

    servicer = SoukAgentGatewayServicer(souk)
    context = _FakeContext(issue_session_token(identity.public_key, "test-signing-secret"))
    request = souk_pb2.PollRequest(agent_ids=[agent_id], wait_seconds=1)

    response = await servicer.PollForWork(request, context)

    assert list(response.pending) == []
