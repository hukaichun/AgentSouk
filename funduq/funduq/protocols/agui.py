from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ag_ui.core import RunAgentInput, RunErrorEvent, RunStartedEvent

from funduq import repo
from funduq.models import AgentRef, LlmRef
from funduq.agui import build_run_agent_input, rewrite_message_ids
from funduq.errors import AgentNotFound, InvalidRunInput, LlmProviderNotFound
from funduq.identity import verify_actor_chain
from funduq.kyok import (
    KyokBinding,
    parse_kyok_opt_in,
    strip_kyok_context,
)
from funduq.props import RESERVED_METADATA_KEYS, build_forwarded_props

if TYPE_CHECKING:
    from funduq.core import Funduq


@dataclass
class EventStream:
    """A live run's AG-UI events, addressable by thread and run id before they are consumed."""

    thread_id: str
    run_id: str
    events: AsyncIterator[dict[str, Any]]

    async def encode(self) -> AsyncIterator[str]:
        async for event in self.events:
            yield json.dumps(event)


@dataclass
class ThreadSnapshot:
    """Returned instead of a new run's `EventStream` when the target thread already has an
    active run that the incoming request isn't resuming; carries the thread's current state,
    including the in-flight run's id."""

    data: dict[str, Any]


class AGUIAdapter:

    def __init__(self, funduq: "Funduq") -> None:
        self._funduq = funduq

    async def run(self, agent: AgentRef, body: RunAgentInput) -> EventStream | ThreadSnapshot:
        """Starts (or resumes) an AG-UI run for `agent`. An unseen `body.thread_id` gets a new
        thread under a **funduq-minted id** — the caller's own id is deliberately not adopted, and
        the `threadId` on every returned event is the authoritative one to continue with (funduq
        owns its record's primary keys; a caller-chosen name has no caller identity to scope it
        to yet — see the design record on conversation naming rights). The caller-supplied
        `run_id` is likewise ignored in favour of funduq's own. A run on a thread
        that already has one in flight is accepted and queued behind it — one turn per thread at
        a time — and its returned stream stays silent until its turn comes; an AG-UI client
        normally holds one session per thread, so a second concurrent run is unusual but not
        refused. A resume with no surviving paused run to target (another caller answered first)
        gets a `ThreadSnapshot` instead of a stream. If the agent is registered but not currently
        served, the run is recorded as failed and the returned stream carries a `RUN_ERROR` event
        rather than hanging. Raises `AgentNotFound` if `agent` isn't registered,
        `LlmProviderNotFound` if a KYOK opt-in names an unknown LLM provider, and
        `InvalidRunInput` if the assembled AG-UI input is invalid."""
        funduq = self._funduq
        async with funduq.session() as session:
            if await repo.get_agent(session, agent) is None:
                raise AgentNotFound(f"agent '{agent}' is not registered")

            metadata = getattr(body, "metadata", None) or {}
            resume = [r.model_dump(mode="json", by_alias=True) for r in body.resume] if body.resume else None

            metadata, verified_subject, verified_actors, actor_chain = await verify_caller(
                session, metadata
            )

            kyok = parse_kyok_opt_in(metadata)
            kyok_ref = kyok.llm_provider if kyok is not None else None
            metadata = strip_kyok_context(metadata)
            if kyok_ref is not None:
                if await repo.get_llm_provider(session, kyok_ref) is None:
                    raise LlmProviderNotFound(f"unknown KYOK LLM provider '{kyok_ref}'")

            thread_id = await repo.ensure_thread(
                session, agent, body.thread_id, metadata=metadata, create_if_missing=True
            )

            # A resume targets the thread's paused (input-required) run
            # specifically — not "the latest active run", which with queued
            # siblings on the thread may be a different, merely queued run.
            paused = (
                await repo.get_paused_run_for_thread(session, thread_id) if resume else None
            )

            input_dump = body.model_dump(mode="json", by_alias=True)
            if isinstance(input_dump.get("metadata"), dict):
                input_dump["metadata"] = strip_kyok_context(input_dump["metadata"])
            if paused is not None:
                run_id = paused["run_id"]
                # Status-guarded so two concurrent resumes resolve to one; the
                # loser sees the thread as busy, same as any other caller.
                if not await repo.reopen_run(
                    session, run_id, input_dump, metadata=metadata,
                    expected_status="input-required",
                ):
                    return ThreadSnapshot(await repo.get_thread_snapshot(session, thread_id))
                starting_seq = await repo.get_last_event_seq(session, run_id)
            else:
                await repo.ensure_queue_room(
                    session, thread_id, funduq.settings.thread_queue_limit
                )
                created = await repo.create_run(
                    session, thread_id, agent, "ag-ui", input_dump, metadata=metadata
                )
                run_id = created["run_id"]
                starting_seq = 0

            raw_messages = [m.model_dump(mode="json", by_alias=True) for m in body.messages]
            messages = await repo.append_thread_messages(session, thread_id, run_id, raw_messages)

            if not funduq.is_serving(agent):
                await funduq.mark_run_status(
                    session, run_id, "failed", metadata={"failureReason": "agent_offline"}
                )
                await session.commit()
                return EventStream(thread_id, run_id, _offline_events(thread_id, run_id))

            try:
                input_json = build_run_agent_input(
                    thread_id,
                    run_id,
                    messages,
                    state=body.state,
                    tools=[t.model_dump(mode="json", by_alias=True) for t in body.tools],
                    context=[c.model_dump(mode="json", by_alias=True) for c in body.context],
                    forwarded_props=build_forwarded_props(
                        funduq.settings.token_signing_secret,
                        run_id,
                        agent,
                        kyok_ref is not None,
                        body.forwarded_props,
                        verified_subject,
                        verified_actors,
                        actor_chain,
                    ),
                    resume=resume,
                )
            except ValueError as e:
                raise InvalidRunInput(str(e)) from e

            await session.commit()

        if kyok_ref is not None:
            funduq.kyok_relay.bind_run(
                run_id,
                KyokBinding(llm_provider=kyok_ref, context=kyok.context, actor_chain=actor_chain),
            )
        funduq.enqueue_run(run_id, agent, thread_id, input_json, "ag-ui", seq=starting_seq)
        return EventStream(thread_id, run_id, _relay(funduq.broker.subscribe(run_id)))


async def _relay(events: AsyncIterator[Any]) -> AsyncIterator[dict[str, Any]]:
    message_id_map: dict[str, str] = {}
    async for item in events:
        yield rewrite_message_ids(item, message_id_map)


async def _offline_events(thread_id: str, run_id: str) -> AsyncIterator[dict[str, Any]]:
    yield RunStartedEvent(thread_id=thread_id, run_id=run_id).model_dump(
        mode="json", by_alias=True, exclude_none=True
    )
    yield RunErrorEvent(message="agent is currently offline").model_dump(
        mode="json", by_alias=True, exclude_none=True
    )


async def verify_caller(session, metadata: dict) -> tuple[dict, Any, list[dict], Any]:
    """Verifies `metadata["actorChain"]` if present, resolving each actor's public key to a
    registered agent name and returning `(metadata with a verifiedActorChain entry added,
    verified subject, verified actors, raw actor chain)`. Raises `InvalidActorChain` if the
    chain is tampered. Returns `metadata` with empty/`None` verification fields if there is
    no actor chain to verify.

    Both doors funnel caller metadata through here, which makes it the one
    place to strip funduq's reserved keys from the caller's input: without the
    strip, a caller could plant a forged `verifiedActorChain` — no chain
    attached, nothing to verify, yet the record would carry a verification
    summary in funduq's handwriting."""
    metadata = {k: v for k, v in metadata.items() if k not in RESERVED_METADATA_KEYS}
    actor_chain = metadata.get("actorChain")
    if not actor_chain:
        return metadata, None, [], None

    result = verify_actor_chain(actor_chain)
    verified_actors: list[dict] = []
    for public_key in result.actor_public_keys:
        resolved = await repo.get_agent_name_for_public_key(session, public_key)
        verified_actors.append({"publicKey": public_key, "agentName": resolved})
    metadata = {
        **metadata,
        "verifiedActorChain": {"subject": result.subject, "actors": verified_actors},
    }
    return metadata, result.subject, verified_actors, actor_chain


