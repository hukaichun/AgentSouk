from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from openai.types.chat import ChatCompletionChunk, CompletionCreateParams

from souk_provider_sdk.identity import ProviderIdentity


@dataclass(frozen=True)
class DeliveredCompletion:

    run_id: str
    provider_key: str
    agent_name: str
    body: CompletionCreateParams
    llm_name: str = ""
    context: Any = None
    actor_chain: list[str] | None = None


CompletionHandler = Callable[[DeliveredCompletion], AsyncIterator[ChatCompletionChunk]]


class CompletionRefused(Exception):
    """Raise from a `CompletionHandler` to answer with a structured refusal instead of an opaque failure.

    `refusal` travels intact through souk's relay to the calling agent — the
    library defines only this envelope, never the vocabulary inside it; what
    the payload means is between this provider and its callers. The attribute
    name is the contract souk reads duck-typed (any exception carrying a
    `refusal` dict), so neither package imports the other. Any other
    exception still collapses to an unstructured failure.
    """

    def __init__(self, refusal: dict[str, Any]) -> None:
        super().__init__(str(refusal))
        self.refusal = refusal


class InProcessLLMProvider:
    """Adapts a `CompletionHandler` to the shape KYOK expects of an attached LLM provider."""

    def __init__(self, identity: ProviderIdentity, llm: CompletionHandler) -> None:
        self._identity = identity
        self._llm = llm

    @property
    def public_key(self) -> str:
        return self._identity.public_key

    def complete(self, request: Any) -> AsyncIterator[ChatCompletionChunk]:
        """Repackages a completion request's fields into a `DeliveredCompletion` and hands it to the LLM."""
        return self._llm(
            DeliveredCompletion(
                run_id=request.run_id,
                provider_key=request.agent.provider_key,
                agent_name=request.agent.name,
                body=request.body,
                llm_name=request.llm_name,
                context=request.context,
                actor_chain=request.actor_chain,
            )
        )
