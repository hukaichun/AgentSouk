from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

from openai.types.chat import ChatCompletionChunk
from pydantic import BaseModel, ConfigDict, Field


class DeliveredCompletion(BaseModel):
    """One completion request as the LLM provider's handler receives it — and the declared wire form of one.

    A transport carries exactly `model_dump(by_alias=True)` of this
    (camelCase keys) and rebuilds it with `model_validate`; the canonical
    frame is published in `docs/contract-vectors.json`. `body` is an
    OpenAI-shaped completion-create object relayed as-is — funduq is a relay,
    not a validator, so it stays a dict here and openai's types are the
    authority on its inside.
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    run_id: str = Field(alias="runId")
    provider_key: str = Field(alias="providerKey")
    agent_name: str = Field(alias="agentName")
    body: dict[str, Any]
    llm_name: str = Field(default="", alias="llmName")
    context: Any = None
    actor_chain: list[str] | None = Field(default=None, alias="actorChain")

    @classmethod
    def from_request(cls, request: Any) -> "DeliveredCompletion":
        """Translates funduq's completion-request object (read by attribute, never imported) into the delivered form."""
        return cls(
            run_id=request.run_id,
            provider_key=request.agent.provider_key,
            agent_name=request.agent.name,
            body=request.body,
            llm_name=request.llm_name,
            context=request.context,
            actor_chain=request.actor_chain,
        )


CompletionHandler = Callable[[DeliveredCompletion], AsyncIterator[ChatCompletionChunk]]


class CompletionRefused(Exception):
    """Raise from a `CompletionHandler` to answer with a structured refusal instead of an opaque failure.

    `refusal` travels intact through funduq's relay to the calling agent — the
    library defines only this envelope, never the vocabulary inside it; what
    the payload means is between this provider and its callers. The attribute
    name is the contract funduq reads duck-typed (any exception carrying a
    `refusal` dict), so neither package imports the other. Any other
    exception still collapses to an unstructured failure.
    """

    def __init__(self, refusal: dict[str, Any]) -> None:
        super().__init__(str(refusal))
        self.refusal = refusal


