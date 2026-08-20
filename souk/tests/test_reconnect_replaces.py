from __future__ import annotations

from souk_llm_provider_sdk import InProcessLLMProvider, ProviderIdentity, sign_llm_registration

from souk.models import AgentRef, LlmRef

from tests.test_llm_provider_drives_kyok import StubLLM


class _StubConnection:

    max_concurrent_runs = None

    def __init__(self, identity: ProviderIdentity) -> None:
        self.public_key = identity.public_key
        self.sign_connect = identity.sign_connect

    async def deliver(self, run):
        return False

    def cancel(self, run_id: str) -> None:
        pass


async def test_a_replaced_agent_connections_cleanup_leaves_the_replacement_serving(souk, register):
    served = await register("worker")
    key = served.identity.public_key
    ref = AgentRef(provider_key=key, name="worker")
    old, new = _StubConnection(served.identity), _StubConnection(served.identity)

    await souk.attach_provider(old, ["worker"])
    await souk.attach_provider(new, ["worker"])
    assert souk.broker.serving(ref) is new

    souk.detach_provider(key, connection=old)
    assert souk.broker.serving(ref) is new

    souk.detach_provider(key, connection=new)
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


async def test_detach_all_for_evicts_the_key_from_both_rosters(souk, register):
    served = await register("evictee")
    identity = served.identity
    key = identity.public_key
    agent_ref = AgentRef(provider_key=key, name="evictee")
    signature, timestamp = sign_llm_registration(identity, ["gpt4"])
    await souk.register_llm_providers(key, signature, timestamp, ["gpt4"])
    llm_ref = LlmRef(provider_key=key, name="gpt4")

    await souk.attach_provider(_StubConnection(identity), ["evictee"])
    await souk.attach_llm_provider(InProcessLLMProvider(identity, StubLLM()), ["gpt4"])
    assert souk.broker.serving(agent_ref) is not None
    assert souk.kyok_relay.serving(llm_ref) is not None

    souk.detach_all_for(key)

    assert souk.broker.serving(agent_ref) is None
    assert souk.kyok_relay.serving(llm_ref) is None
