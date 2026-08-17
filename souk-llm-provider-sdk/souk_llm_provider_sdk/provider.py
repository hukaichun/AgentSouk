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
