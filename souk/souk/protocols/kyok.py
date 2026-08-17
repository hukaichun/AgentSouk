"""KYOK translation: relaying a provider's LLM call to whoever is paying.

KYOK is structurally a second broker — an agent provider submits work
(a completion), an LLM provider answers it — so it splits the same way
runs do: the mechanism and its checks live in core, the agent-facing
endpoint is serving. The answering side is not an endpoint at all any
more: a completion is delivered to the `ConnectedLLMProvider` the run
was bound to at start (souk.kyok.KyokRelay), so the poll/respond
surface this module used to carry is gone, and with it the rendezvous
state it existed to serve.

What this deliberately holds rather than leaving to a route: the two-part
authorization. A KYOK token proves souk minted it and names a run; it does
not prove who is presenting it. The call-time signature is the other half,
and getting either wrong means someone else spending the caller's real LLM
budget — not the kind of check to reimplement per transport.

No claim timeout, and none is needed: there is no claim step. The old
30s constant was applied to every inter-chunk gap and killed any model
slower than that to its next token, while reporting "no bridge claimed
this" — a message that was false in both halves. A bridge that hangs
mid-stream is now the same problem as any hung upstream call, and it
belongs to whoever is doing the waiting: the provider's own HTTP client
timeout, or the serving layer cancelling the relay when the provider
disconnects. Core inventing a number here would just be the old defect
with a new name.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

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
    """A completion being answered by the run's own bridge.

    The bridge signals failure by raising (see souk.kyok.LLMBridge); what
    that looks like to the provider depends on which shape it asked for,
    which is why the two consumers below differ:

    - `collapsed` (non-streaming): no bytes have been sent yet, so a
      failure can still be an honest status — it becomes KyokRejected 502.
    - `encode` (streaming): the 200 is long gone, so the failure goes
      in-band as a final `{"error": ...}` payload, the shape OpenAI's own
      streaming API uses mid-stream, and the stream ends without [DONE].
    """

    stream_requested: bool
    chunks: AsyncIterator[ChatCompletionChunk]

    async def collapsed(self) -> ChatCompletion:
        """Every chunk reassembled into one non-streaming response, for a
        provider that didn't ask to stream."""
        try:
            collected = [chunk async for chunk in self.chunks]
        except Exception as e:
            raise KyokRejected(f"KYOK bridge failed to complete: {e}", status=502) from e
        return collapse_stream(collected)

    async def encode(self) -> AsyncIterator[str]:
        """The chunks as OpenAI-style SSE payloads, terminator included."""
        try:
            async for chunk in self.chunks:
                yield chunk.model_dump_json()
        except Exception as e:
            logger.warning("KYOK bridge failed mid-stream: %s", e)
            yield json.dumps({"error": {"message": str(e)}})
            return
        yield "[DONE]"


class KyokAdapter:
    """KYOK semantics over a Souk."""

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
        """A provider's OpenAI-shaped call, authorized and relayed.

        Three checks, all here rather than in a route:

        1. the KYOK token is souk's own and unexpired,
        2. the run it names is *still genuinely in flight* for that agent —
           which caps a leaked token's usable window to the life of that run,
           rather than leaving the token's own hour-long TTL as the only thing
           between a finished run and someone spending the caller's budget,
        3. the call is signed by the agent's own key, over this
           exact token + timestamp + body, so a signature can't be replayed
           onto another request.

        Then the run's LLM provider answers it — the one the caller named
        at run start, resolved by run_id (which only souk ever mints) to a
        name, and by name to whichever connection that identity has
        attached *right now*. Offline is 503: the run may well still be
        going, but its LLM is unreachable, and that is exactly what the
        agent provider is told — the same fast-fail shape as an offline
        agent, nothing invented.
        """
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
            # After the authorization checks, not before: what a caller may
            # learn about its own malformed request is a different question
            # from whether it was allowed to ask at all.
            raise KyokRejected("KYOK completion body is not valid JSON", status=400) from e

        binding = self._souk.kyok_relay.binding_for(token.run_id)
        if binding is None:
            # The binding dies with the run (broker's forget funnel), so a
            # token that passed the live-run check a moment ago can still
            # land here in the narrow window where the run just ended.
            raise KyokRejected("run has no KYOK binding any more", status=503)
        link = self._souk.kyok_relay.serving(binding.llm_provider)
        if link is None:
            raise KyokRejected(
                f"LLM provider '{binding.llm_provider}' is not attached", status=503
            )

        return CompletionRelay(
            stream_requested=bool(payload.get("stream")),
            chunks=link.complete(
                CompletionRequest(
                    run_id=token.run_id,
                    agent=token.agent,
                    body=payload,
                    llm_name=binding.llm_provider.name,
                    context=binding.context,
                    actor_chain=binding.actor_chain,
                )
            ),
        )

    async def _verify_caller(
        self, token: KyokToken, bearer: str, body: bytes, timestamp: str, signature: str
    ) -> None:
        """The half the token can't cover: souk_agent_sdk.KyokSigningAuth
        signs every call with the calling provider's registration identity, so
        the key is the token's own — souk being the
        one source of truth for that — and checks the signature really came
        from it. Reuses souk.identity's freshness window rather than a second
        constant; no reason KYOK's should ever diverge.
        """
        if not timestamp or not signature:
            raise KyokRejected("missing KYOK call-time signature", status=401)
        try:
            fresh = is_timestamp_fresh(int(timestamp))
        except ValueError as e:
            raise KyokRejected("malformed KYOK signature timestamp", status=401) from e
        if not fresh:
            raise KyokRejected("KYOK signature timestamp is stale", status=401)

        # Still asked, because a de-listed agent must not keep spending a
        # caller's key — but no longer asked in order to *learn* the key.
        # The token carries it: an agent is `(provider_key, name)`, souk
        # signed the token itself, and there used to be a whole lookup here
        # (`repo.get_agent_public_key`) whose only job was turning an id back
        # into the identity it always stood for.
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
    """Reassembles streaming chunks into one non-streaming response, for a
    provider that called with `stream` unset or false. A bridge always
    produces streaming-shaped chunks (see souk.kyok.LLMBridge) regardless
    of what the provider asked for — this is where a non-streaming caller
    gets that collapsed back down instead of needing its own merge logic.

    Field names come from the `openai` package on both sides of this
    function, so a rename there fails here at type-check and construction
    time rather than silently producing a shape no client parses.
    """
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
