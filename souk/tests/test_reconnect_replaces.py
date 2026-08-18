from __future__ import annotations

from souk_llm_provider_sdk import InProcessLLMProvider, ProviderIdentity, sign_llm_registration

from souk.models import AgentRef, LlmRef

from tests.test_llm_provider_drives_kyok import StubLLM


class _StubConnection:

    max_concurrent_runs = None

    def __init__(self, public_key: str) -> None:
        self.public_key = public_key

    async def deliver(self, run):
        return False

    def cancel(self, run_id: str) -> None:
        pass


async def test_a_replaced_agent_connections_cleanup_leaves_the_replacement_serving(souk, register):
    served = await register("worker")
    key = served.identity.public_key
    ref = AgentRef(provider_key=key, name="worker")
    old, new = _StubConnection(key), _StubConnection(key)

    await souk.attach_provider(old, ["worker"])
    await souk.attach_provider(new, ["worker"])
    assert souk.broker.serving(ref) is new

    await souk.detach_provider(key, connection=old)
    assert souk.broker.serving(ref) is new

    await souk.detach_provider(key, connection=new)
    assert souk.broker.serving(ref) is None


async def test_a_replaced_llm_connections_cleanup_leaves_the_replacement_serving(souk):
    identity = ProviderIdentity.generate()
    signature, timestamp = sign_llm_registration(identity, ["gpt4"])
    await souk.register_llm_providers(identity.public_key, signature, timestamp, ["gpt4"])
    ref = LlmRef(provider_key=identity.public_key, name="gpt4")
    old = InProcessLLMProvider(identity, StubLLM())
    new = InProcessLLMProvider(identity, StubLLM())

    await souk.attach_llm_provider(old, ["gpt4"])
    await souk.attach_llm_provider(new, ["gpt4"])
    assert souk.kyok_relay.serving(ref) is new

    souk.detach_llm_provider(identity.public_key, connection=old)
    assert souk.kyok_relay.serving(ref) is new

    souk.detach_llm_provider(identity.public_key, connection=new)
    assert souk.kyok_relay.serving(ref) is None
