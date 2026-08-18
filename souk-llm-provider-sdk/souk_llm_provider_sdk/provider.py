from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from openai.types.chat import ChatCompletionChunk, CompletionCreateParams


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


