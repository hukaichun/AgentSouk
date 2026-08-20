from __future__ import annotations

from collections.abc import AsyncIterator

from openai.types.chat import ChatCompletionChunk

from souk_provider_sdk.identity import (
    ProviderIdentity,
    WrongSouk,
    souk_connect_payload,
    verify_signature,
)

from souk_llm_provider_sdk.link import SoukLLMLink
from souk_llm_provider_sdk.provider import CompletionHandler, DeliveredCompletion


class InProcessLLMProvider(SoukLLMLink):
    """The in-process transport: a `SoukLLMLink` that drives a `CompletionHandler` directly."""

    def __init__(
        self,
        identity: ProviderIdentity,
        llm: CompletionHandler,
        souk_public_key: str | None = None,
    ) -> None:
        self._identity = identity
        self._llm = llm
        self._souk_public_key = souk_public_key

    @property
    def public_key(self) -> str:
        return self._identity.public_key

    def sign_connect(self, souk_nonce: str, provider_nonce: str, names: list[str]) -> str:
        return self._identity.sign_connect(souk_nonce, provider_nonce, names)

    def confirm_connect(self, souk_nonce: str, provider_nonce: str, answer: str | None) -> None:
        """Verify souk's answering signature against `souk_public_key`, raising `WrongSouk` on a miss; a no-op when no key was pinned."""
        if self._souk_public_key is None:
            return
        if answer is None or not verify_signature(
            self._souk_public_key, answer, souk_connect_payload(souk_nonce, provider_nonce)
        ):
            raise WrongSouk(
                f"the souk answering this link-open did not prove '{self._souk_public_key}'"
            )

    def serve(self, delivered: DeliveredCompletion) -> AsyncIterator[ChatCompletionChunk]:
        return self._llm(delivered)
