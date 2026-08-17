"""What an LLM provider is, and what souk asks of it.

One method: a completion request in, streaming chunks out. The chunks are
always streaming-shaped whatever the agent provider asked for — collapsing
for a non-streaming caller is souk's job, done once on its side.

Policy lives here and nowhere else. souk hands over who is asking
(`DeliveredCompletion`) and decides nothing: whether to serve, to
throttle, to bill, or to refuse is this party's own business, expressed
by answering or by raising. A spend ceiling is a policy too, which is why
it belongs in the `llm` callable an integrator supplies rather than
anywhere in souk.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from openai.types.chat import ChatCompletionChunk, CompletionCreateParams

from souk_provider_sdk.identity import ProviderIdentity


@dataclass(frozen=True)
class DeliveredCompletion:
    """One completion handed to this LLM provider — this package's own
    type, on purpose (see souk-provider-sdk's DeliveredRun for the
    AttributeError that made this a rule): the policy callable reads
    this, and building one from whatever souk actually sends is the
    adapter's job — the one place entitled to know both shapes.

    `provider_key`/`agent_name` are the calling *agent* provider's
    identity pair, already proven by its call-time signature before souk
    delivers this. `llm_name` is which of this provider's own offerings
    was addressed — one connection may serve several. `context` is
    whatever credential the run's caller presented to *this* LLM provider
    (opaque to souk, this pair's own vocabulary; None if none), and
    `actor_chain` is the raw hop-signed delegation path that reached the
    run — each hop verifiable against a registered provider key, so
    policy like "serve only chains through providers I expect" needs no
    trust in souk's summary. Together they are what abuse policy keys on.
    """

    run_id: str
    provider_key: str
    agent_name: str
    body: CompletionCreateParams
    llm_name: str = ""
    context: Any = None
    actor_chain: list[str] | None = None


CompletionHandler = Callable[[DeliveredCompletion], AsyncIterator[ChatCompletionChunk]]


class InProcessLLMProvider:
    """An LLM provider and a souk in one process, joined.

    Satisfies souk's `ConnectedLLMProvider` protocol structurally —
    `public_key` and `complete` — without importing souk, the same way
    souk_provider_sdk.InProcessLink satisfies `ConnectedProvider`.
    In-process is a transport, not a special case: this object registers
    (sign_llm_registration), attaches, and is resolved per completion
    exactly as a remote one would be.

    `request` arrives as souk's own CompletionRequest and is read by
    attribute here — the one adapter seam that knowingly names both
    sides — so the `llm` callable only ever sees this package's
    DeliveredCompletion.
    """

    def __init__(self, identity: ProviderIdentity, llm: CompletionHandler) -> None:
        self._identity = identity
        self._llm = llm

    @property
    def public_key(self) -> str:
        return self._identity.public_key

    def complete(self, request: Any) -> AsyncIterator[ChatCompletionChunk]:
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
