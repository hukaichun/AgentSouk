from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from openai.types.chat import ChatCompletion, ChatCompletionChunk, CompletionCreateParams
from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_message import ChatCompletionMessage

from souk import repo
from souk.errors import KyokRejected
from souk.identity import (
    is_timestamp_fresh,
    kyok_call_signing_payload,
    verify_signature,
)
from souk.kyok import CompletionRequest, KyokToken, verify_kyok_token

if TYPE_CHECKING:
    from souk.core import Souk

logger = logging.getLogger(__name__)


@dataclass
class CompletionRelay:
    """A completion in flight from an attached LLM provider back to the KYOK caller, either
    consumed in one shot as an OpenAI `ChatCompletion` (`collapsed`) or streamed out as
    server-sent chunks (`encode`). Any error raised while draining `chunks` is surfaced as
    `KyokRejected` (status 502) from `collapsed`, or as an inline `{"error": ...}` payload
    followed by end-of-stream from `encode`.

    An error carrying a `refusal` dict (the LLM provider's structured
    refusal — `souk_llm_provider_sdk.CompletionRefused` builds these; the
    attribute name is the contract, read duck-typed) travels intact: it
    becomes the `{"error": ...}` payload in-stream, or rides `KyokRejected.
    refusal`, instead of being flattened to prose. souk relays the payload
    and never interprets it — its vocabulary belongs to the LLM provider
    and its callers."""

    stream_requested: bool
    chunks: AsyncIterator[ChatCompletionChunk]

    async def collapsed(self) -> ChatCompletion:
        try:
            collected = [chunk async for chunk in self.chunks]
        except Exception as e:
            raise KyokRejected(
                f"KYOK bridge failed to complete: {e}",
                status=502,
                refusal=_refusal_of(e),
            ) from e
        return collapse_stream(collected)

    async def encode(self) -> AsyncIterator[str]:
        try:
            async for chunk in self.chunks:
                yield chunk.model_dump_json()
        except Exception as e:
            logger.warning("KYOK bridge failed mid-stream: %s", e)
            refusal = _refusal_of(e)
            yield json.dumps({"error": refusal if refusal is not None else {"message": str(e)}})
            return
        yield "[DONE]"


def _refusal_of(e: Exception) -> dict[str, Any] | None:
    refusal = getattr(e, "refusal", None)
    return refusal if isinstance(refusal, dict) else None


class KyokAdapter:

    def __init__(self, souk: "Souk") -> None:
        self._souk = souk

    async def complete(
        self,
        bearer: str,
        body: bytes,
        *,
        timestamp: str,
        signature: str,
    ) -> CompletionRelay:
        """Authenticates a KYOK completion call and forwards it to the bound LLM provider.
        `bearer` must be a valid, unexpired KYOK token for a run that is still active (not
        cancelled) for the agent named in the token, and `timestamp`/`signature` must be a
        fresh, correctly signed proof that the calling agent itself made this call — else raises
        `KyokRejected` with 401 or 403. Raises `KyokRejected` (400) if `body` isn't valid JSON,
        and (503) if the run's bound LLM provider is no longer attached."""
        token = verify_kyok_token(bearer, self._souk.settings.token_signing_secret)
        if token is None:
            raise KyokRejected("invalid or expired KYOK token", status=401)

        run = self._souk.broker.get(token.run_id)
        if run is None or run.cancel_requested or run.agent != token.agent:
            raise KyokRejected("run is not currently active for this token", status=403)

        await self._verify_caller(token, bearer, body, timestamp, signature)

        try:
            payload = cast(CompletionCreateParams, json.loads(body))
        except json.JSONDecodeError as e:
            raise KyokRejected("KYOK completion body is not valid JSON", status=400) from e

        binding = self._souk.kyok_relay.binding_for(token.run_id)
        if binding is None:
            raise KyokRejected("run has no KYOK binding any more", status=503)
        link = self._souk.kyok_relay.serving(binding.llm_provider)
        if link is None:
            raise KyokRejected(
                f"LLM provider '{binding.llm_provider}' is not attached", status=503
            )

        return CompletionRelay(
            stream_requested=bool(payload.get("stream")),
            chunks=self._counted(
                binding.llm_provider.provider_key,
                link.complete(
                    CompletionRequest(
                        run_id=token.run_id,
                        agent=token.agent,
                        body=payload,
                        llm_name=binding.llm_provider.name,
                        context=binding.context,
                        actor_chain=binding.actor_chain,
                    )
                ),
            ),
        )

    async def _counted(
        self, public_key: str, chunks: AsyncIterator[ChatCompletionChunk]
    ) -> AsyncIterator[ChatCompletionChunk]:
        """Passes `chunks` through while recording what souk observed into the relay's quality counters."""
        try:
            async for chunk in chunks:
                yield chunk
        except Exception as e:
            self._souk.kyok_relay.note_outcome(
                public_key, "refused" if _refusal_of(e) is not None else "failed"
            )
            raise
        self._souk.kyok_relay.note_outcome(public_key, "completions")

    async def _verify_caller(
        self, token: KyokToken, bearer: str, body: bytes, timestamp: str, signature: str
    ) -> None:
        """Raises `KyokRejected` unless `signature` is a fresh, valid signature — by the agent
        named in `token`, using its registered public key — over the bearer token, timestamp,
        and a hash of the request body."""
        if not timestamp or not signature:
            raise KyokRejected("missing KYOK call-time signature", status=401)
        try:
            fresh = is_timestamp_fresh(int(timestamp))
        except ValueError as e:
            raise KyokRejected("malformed KYOK signature timestamp", status=401) from e
        if not fresh:
            raise KyokRejected("KYOK signature timestamp is stale", status=401)

        async with self._souk.session() as session:
            registered = await repo.get_agent(session, token.agent)
        if registered is None:
            raise KyokRejected(f"agent '{token.agent}' is not registered", status=403)

        payload = kyok_call_signing_payload(
            bearer, int(timestamp), hashlib.sha256(body).hexdigest()
        )
        if not verify_signature(token.agent.provider_key, signature, payload):
            raise KyokRejected("KYOK call-time signature verification failed", status=401)


def collapse_stream(chunks: list[ChatCompletionChunk]) -> ChatCompletion:
    """Merges a sequence of `ChatCompletionChunk`s into a single `ChatCompletion`, concatenating
    each choice index's content deltas and taking that index's last non-empty finish reason
    (defaulting to `"stop"`). An empty chunk list collapses to one empty assistant message with
    finish reason `"stop"`."""
    if not chunks:
        return ChatCompletion(
            id="",
            object="chat.completion",
            created=0,
            model="",
            choices=[
                Choice(
                    index=0,
                    message=ChatCompletionMessage(role="assistant", content=""),
                    finish_reason="stop",
                )
            ],
        )

    first = chunks[0]
    content_by_index: dict[int, str] = {}
    finish_reason_by_index: dict[int, str] = {}
    for chunk in chunks:
        for chunk_choice in chunk.choices:
            index = chunk_choice.index
            content_by_index[index] = content_by_index.get(index, "") + (
                chunk_choice.delta.content or ""
            )
            if chunk_choice.finish_reason:
                finish_reason_by_index[index] = chunk_choice.finish_reason

    return ChatCompletion(
        id=first.id,
        object="chat.completion",
        created=first.created,
        model=first.model,
        choices=[
            Choice(
                index=index,
                message=ChatCompletionMessage(role="assistant", content=content),
                finish_reason=finish_reason_by_index.get(index, "stop"),
            )
            for index, content in sorted(content_by_index.items())
        ],
    )
