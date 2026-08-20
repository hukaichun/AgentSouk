"""The wire, simulated without a transport.

Every crossing in these tests is serialized to JSON bytes and rebuilt on
the far side from the published frame shapes — exactly what a real
transport carries — with no socket anywhere. This is the proof behind the
docs' claim that the SDKs let you exercise the wire without touching a
transport: if a frame shape drifts, this file fails the way a deployed
gateway would.
"""

from __future__ import annotations

import asyncio
import json

from ag_ui.core import RunAgentInput, UserMessage
from openai.types.chat import ChatCompletionChunk

from funduq_llm_provider_sdk import DeliveredCompletion, sign_llm_registration
from funduq_provider_sdk import (
    AgentHandle,
    DeliveredRun,
    HandleProvider,
    ProviderIdentity,
    ProviderRuntime,
    Refusal,
)

from funduq.kyok import CompletionRequest
from funduq.models import LlmRef
from funduq.protocols.agui import AGUIAdapter


class WireLink:
    """A ConnectedProvider whose every crossing is JSON bytes, not objects."""

    def __init__(self, funduq, runtime: ProviderRuntime) -> None:
        self._funduq = funduq
        self._runtime = runtime
        runtime.link = self
        self.public_key = runtime.public_key
        self.max_concurrent_runs = runtime.max_concurrent_runs

    def sign_connect(
        self, funduq_public_key: str, funduq_nonce: str, provider_nonce: str, names: list[str]
    ) -> str:
        # the claimed funduq key, the challenge and the proof all cross as bytes too
        relayed_key = funduq_public_key.encode().decode()
        relayed = funduq_nonce.encode().decode()
        return self._runtime.identity.sign_connect(relayed_key, relayed, provider_nonce, names)

    async def deliver(self, run) -> bool | Refusal:
        frame = DeliveredRun.from_claimed(run).model_dump_json(by_alias=True).encode()
        delivered = DeliveredRun.model_validate_json(frame)
        accepted = await self._runtime.deliver(delivered)
        answer = json.dumps({"accepted": bool(accepted)}).encode()
        return json.loads(answer)["accepted"]

    def cancel(self, run_id: str) -> None:
        self._runtime.cancel(json.loads(json.dumps(run_id)))

    async def report_event(self, run_id: str, event) -> None:
        frame = json.dumps({"runId": run_id, "event": event}).encode()
        decoded = json.loads(frame)
        self._funduq.report_event(decoded["runId"], decoded["event"], claimed_by=self.public_key)

    async def finish_run(self, run_id: str) -> None:
        self._funduq.finish_run(json.loads(json.dumps(run_id)), claimed_by=self.public_key)

    async def thread_messages(self, thread_id: str, *, limit: int | None = None):
        raw = await self._funduq.get_thread_messages(thread_id)
        return json.loads(json.dumps(raw))


async def test_a_run_travels_as_byte_frames_end_to_end(funduq):
    async def agent(run_input: RunAgentInput):
        ids = {"threadId": run_input.thread_id, "runId": run_input.run_id}
        yield {"type": "RUN_STARTED", **ids}
        yield {
            "type": "TEXT_MESSAGE_CONTENT",
            "messageId": "m-out",
            "delta": f"caller={((run_input.forwarded_props or {}).get('caller') or {}).get('subject')}",
        }
        yield {"type": "RUN_FINISHED", **ids}

    identity = ProviderIdentity.generate()
    signature, timestamp = identity.sign_registration(["wired"])
    registration = await funduq.register_agents(
        identity.public_key, signature, timestamp, [{"name": "wired"}]
    )
    runtime = ProviderRuntime(identity, HandleProvider([AgentHandle("wired", agent)]))
    runtime.start()
    link = WireLink(funduq, runtime)
    try:
        await funduq.attach_provider(link, ["wired"])

        stream = await AGUIAdapter(funduq).run(
            registration.agents["wired"],
            RunAgentInput(
                thread_id="t-wire",
                run_id="ignored",
                state={},
                messages=[UserMessage(id="m1", role="user", content="hi")],
                tools=[],
                context=[],
                forwarded_props={},
            ),
        )
        async with asyncio.timeout(5):
            events = [e async for e in stream.events]
    finally:
        await runtime.aclose(cancel_in_flight=True)

    assert [e["type"] for e in events] == [
        "RUN_STARTED",
        "TEXT_MESSAGE_CONTENT",
        "RUN_FINISHED",
    ]


class WireLLMLink:
    """A ConnectedLLMProvider whose request and chunks cross as JSON bytes."""

    def __init__(self, identity: ProviderIdentity) -> None:
        self._identity = identity
        self.public_key = identity.public_key

    def sign_connect(
        self, funduq_public_key: str, funduq_nonce: str, provider_nonce: str, names: list[str]
    ) -> str:
        return self._identity.sign_connect(funduq_public_key, funduq_nonce, provider_nonce, names)

    def complete(self, request: CompletionRequest):
        frame = DeliveredCompletion.from_request(request).model_dump_json(by_alias=True).encode()
        delivered = DeliveredCompletion.model_validate_json(frame)

        async def _chunks():
            chunk = ChatCompletionChunk.model_validate(
                {
                    "id": "chatcmpl-wire",
                    "object": "chat.completion.chunk",
                    "created": 1755300000,
                    "model": delivered.body["model"],
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": f"for {delivered.agent_name}"},
                            "finish_reason": "stop",
                        }
                    ],
                }
            )
            wire = chunk.model_dump_json().encode()
            yield ChatCompletionChunk.model_validate_json(wire)

        return _chunks()


async def test_a_completion_travels_as_byte_frames(funduq):
    identity = ProviderIdentity.generate()
    signature, timestamp = sign_llm_registration(identity, ["wire-model"])
    await funduq.register_llm_providers(identity.public_key, signature, timestamp, ["wire-model"])
    link = WireLLMLink(identity)
    await funduq.attach_llm_provider(link, ["wire-model"])

    ref = LlmRef(provider_key=identity.public_key, name="wire-model")
    serving = funduq.kyok_relay.serving(ref)
    request = CompletionRequest(
        run_id="run-wire",
        agent=registration_ref(identity),
        body={"model": "wire-model", "messages": [{"role": "user", "content": "hi"}]},
        llm_name="wire-model",
    )
    chunks = [chunk async for chunk in serving.complete(request)]

    funduq.detach_all_for(identity.public_key)
    assert chunks[0].choices[0].delta.content == "for wired-agent"


def registration_ref(identity: ProviderIdentity):
    from funduq.models import AgentRef

    return AgentRef(provider_key=identity.public_key, name="wired-agent")
