from __future__ import annotations

from collections.abc import AsyncIterator

from openai.types.chat import ChatCompletionChunk

from souk_provider_sdk.identity import ProviderIdentity

from souk_llm_provider_sdk.link import SoukLLMLink
from souk_llm_provider_sdk.provider import CompletionHandler, DeliveredCompletion


class InProcessLLMProvider(SoukLLMLink):
    """The in-process transport: a `SoukLLMLink` that drives a `CompletionHandler` directly."""

    def __init__(self, identity: ProviderIdentity, llm: CompletionHandler) -> None:
        self._identity = identity
        self._llm = llm

    @property
    def public_key(self) -> str:
        return self._identity.public_key

    def sign_connect(self, souk_nonce: str, provider_nonce: str, names: list[str]) -> str:
        return self._identity.sign_connect(souk_nonce, provider_nonce, names)

    def serve(self, delivered: DeliveredCompletion) -> AsyncIterator[ChatCompletionChunk]:
        return self._llm(delivered)
