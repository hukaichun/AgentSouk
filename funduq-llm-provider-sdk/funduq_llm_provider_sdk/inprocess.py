from __future__ import annotations

from collections.abc import AsyncIterator

from openai.types.chat import ChatCompletionChunk

from funduq_provider_sdk.identity import (
    ProviderIdentity,
    WrongFunduq,
    funduq_connect_payload,
    verify_signature,
)

from funduq_llm_provider_sdk.link import FunduqLLMLink
from funduq_llm_provider_sdk.provider import CompletionHandler, DeliveredCompletion


class InProcessLLMProvider(FunduqLLMLink):
    """The in-process transport: a `FunduqLLMLink` that drives a `CompletionHandler` directly."""

    def __init__(
        self,
        identity: ProviderIdentity,
        llm: CompletionHandler,
        funduq_public_key: str | None = None,
    ) -> None:
        self._identity = identity
        self._llm = llm
        self._funduq_public_key = funduq_public_key

    @property
    def public_key(self) -> str:
        return self._identity.public_key

    def sign_connect(
        self, funduq_public_key: str, funduq_nonce: str, provider_nonce: str, names: list[str]
    ) -> str:
        """Sign the link-open proof, bound to the pinned funduq key — refusing, before any
        signature leaves, a funduq claiming a different key than the pin."""
        if self._funduq_public_key is not None and funduq_public_key != self._funduq_public_key:
            raise WrongFunduq(
                f"this link is pinned to '{self._funduq_public_key}', "
                f"not the funduq claiming '{funduq_public_key}'"
            )
        return self._identity.sign_connect(funduq_public_key, funduq_nonce, provider_nonce, names)

    def confirm_connect(self, funduq_nonce: str, provider_nonce: str, answer: str | None) -> None:
        """Verify funduq's answering signature against `funduq_public_key`, raising `WrongFunduq` on a miss; a no-op when no key was pinned."""
        if self._funduq_public_key is None:
            return
        if answer is None or not verify_signature(
            self._funduq_public_key, answer, funduq_connect_payload(funduq_nonce, provider_nonce)
        ):
            raise WrongFunduq(
                f"the funduq answering this link-open did not prove '{self._funduq_public_key}'"
            )

    def serve(self, delivered: DeliveredCompletion) -> AsyncIterator[ChatCompletionChunk]:
        return self._llm(delivered)
