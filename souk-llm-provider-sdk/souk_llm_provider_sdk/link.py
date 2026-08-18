from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from openai.types.chat import ChatCompletionChunk

from souk_llm_provider_sdk.provider import DeliveredCompletion


class SoukLLMLink(ABC):
    """A transport connecting an LLM provider to souk — the peer of `souk_provider_sdk.SoukLink`.

    In-process is a transport, not a special case: `InProcessLLMProvider` is
    one subclass, a socket is another. The base states the translation once —
    souk's completion request becomes a `DeliveredCompletion` before the
    provider's own code sees it — and `serve` is the interposition point:
    every completion a run's agent asks for passes through it before any
    money moves, which is where a caller enforces its own policy (see the
    package README; the library defines the channel, never the policy).
    """

    @property
    @abstractmethod
    def public_key(self) -> str:
        pass

    def complete(self, request: Any) -> AsyncIterator[ChatCompletionChunk]:
        """Repackages a completion request's fields into a `DeliveredCompletion` and hands it to `serve`."""
        return self.serve(
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

    @abstractmethod
    def serve(self, delivered: DeliveredCompletion) -> AsyncIterator[ChatCompletionChunk]:
        pass
