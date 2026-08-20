from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from a2a.types import a2a_pb2 as pb
from a2a.utils.constants import PROTOCOL_VERSION_CURRENT, TransportProtocol
from google.protobuf.json_format import ParseDict, ParseError

from funduq import repo
from funduq.agui import build_run_agent_input
from funduq.errors import AgentNotFound, InvalidRunInput, LlmProviderNotFound, RunNotFound
from funduq.kyok import KyokBinding, parse_kyok_opt_in, strip_kyok_context
from funduq.props import ADDRESSED_RUN_METADATA_KEY, build_forwarded_props
from funduq.models import AgentRef
from funduq.protocols.a2a_translate import (
    a2a_message_to_agui_messages,
    agui_event_to_a2a_update,
    build_task,
    status_update_for_run_status,
    to_wire,
)
from funduq.protocols.agui import verify_caller

if TYPE_CHECKING:
    from funduq.core import Funduq

logger = logging.getLogger("funduq.protocols.a2a")

METHOD_NOT_FOUND = -32601
TASK_NOT_FOUND = -32001

PROTOCOL_VERSION = PROTOCOL_VERSION_CURRENT

_A2A_METHODS = {method.name for method in pb.DESCRIPTOR.services_by_name["A2AService"].methods}


def _method(name: str) -> str:
    """Returns `name` if it is a current `A2AService` RPC method, else raises `RuntimeError` —
    a guard against the dispatch tables below going stale relative to the installed A2A spec."""
    if name not in _A2A_METHODS:
        raise RuntimeError(
            f"A2AService has no method {name!r} — the spec moved and funduq's dispatch is stale. "
            f"It offers: {sorted(_A2A_METHODS)}"
        )
    return name


SEND = frozenset({_method("SendMessage"), "message/send", "tasks/send"})
STREAM = frozenset({_method("SendStreamingMessage"), "message/stream", "tasks/sendSubscribe"})
GET = frozenset({_method("GetTask"), "tasks/get"})
CANCEL = frozenset({_method("CancelTask"), "tasks/cancel"})
SUBSCRIBE = frozenset({_method("SubscribeToTask"), "tasks/resubscribe"})


@dataclass
class A2AStream:

    results: AsyncIterator[dict[str, Any]]

    async def encode(self) -> AsyncIterator[str]:
        async for item in self.results:
            yield json.dumps(item)


_BINDINGS = frozenset(member.name for member in TransportProtocol)


@dataclass(frozen=True)
class ServedInterface:
    """A URL where an agent's A2A endpoint is reachable, together with the A2A transport
    binding it speaks (e.g. `"JSONRPC"`). Raises `ValueError` if `binding` is not a name A2A's
    `TransportProtocol` defines."""

    url: str
    binding: str

    def __post_init__(self) -> None:
        if self.binding not in _BINDINGS:
            raise ValueError(
                f"unknown A2A binding {self.binding!r} — A2A defines {sorted(_BINDINGS)}"
            )


class A2AAdapter:

    def __init__(self, funduq: "Funduq") -> None:
        self._funduq = funduq

    async def agent_card(
        self, agent: AgentRef, interfaces: "list[ServedInterface] | None" = None
    ) -> dict[str, Any]:
        """Builds the A2A agent card for `agent` as wire JSON, listing `interfaces` as its
        supported interfaces (omitted from the card entirely if none are given). Raises
        `AgentNotFound` if `agent` isn't registered."""
        record = await self._funduq.get_agent(agent)
        if record is None:
            raise AgentNotFound(f"agent '{agent}' is not registered")
        card = dict(record.agent_card)
        return to_wire(
            pb.AgentCard(
                name=card.get("name", record.name),
                description=card.get("description", ""),
                version=card.get("version", "0.1.0"),
                supported_interfaces=[
                    pb.AgentInterface(
                        url=served.url,
                        protocol_binding=TransportProtocol[served.binding].value,
                        protocol_version=PROTOCOL_VERSION,
                    )
                    for served in (interfaces or [])
                ],
                capabilities=pb.AgentCapabilities(streaming=True),
                default_input_modes=["text/plain"],
                default_output_modes=["text/plain"],
                skills=_skills(card.get("skills", [])),
            )
        )

    async def handle_rpc(self, agent: AgentRef, payload: dict[str, Any]) -> dict[str, Any] | A2AStream:
        """Dispatches a JSON-RPC A2A request to the matching operation, recognizing each method
        under every name it has had across spec versions (current `A2AService` names as well as
        `message/send`-style legacy names). Returns a JSON-RPC error envelope with code -32601
        for an unrecognized method, or -32001 if the referenced task doesn't exist."""
        method = payload.get("method")
        params = payload.get("params", {})
        rpc_id = payload.get("id")

        if method in SEND:
            return await self._envelope(rpc_id, self.send_task(agent, **_send_args(params)))
        if method in STREAM:
            return await self._envelope_stream(rpc_id, params, agent)
        if method in GET:
            return await self._envelope(rpc_id, self.get_task(agent, params.get("id")))
        if method in CANCEL:
            return await self._envelope(rpc_id, self.cancel_task(agent, params.get("id")))
        if method in SUBSCRIBE:
            return await self._envelope_resubscribe(rpc_id, params, agent)
        return _error(rpc_id, METHOD_NOT_FOUND, f"method not found: {method}")

    async def _envelope_stream(
        self, rpc_id: Any, params: dict[str, Any], agent: AgentRef
    ) -> dict[str, Any] | A2AStream:
        try:
            stream = await self.send_task_streaming(agent, **_send_args(params))
        except RunNotFound:
            return _error(rpc_id, TASK_NOT_FOUND, "task not found")
        return A2AStream(_wrap(rpc_id, stream))

    async def _envelope_resubscribe(
        self, rpc_id: Any, params: dict[str, Any], agent: AgentRef
    ) -> dict[str, Any] | A2AStream:
        try:
            stream = await self.resubscribe_task(agent, params.get("id"))
        except RunNotFound:
            return _error(rpc_id, TASK_NOT_FOUND, "task not found")
        return A2AStream(_wrap(rpc_id, stream))

    async def _envelope(self, rpc_id: Any, coro) -> dict[str, Any]:
        try:
            return _result(rpc_id, await coro)
        except RunNotFound:
            return _error(rpc_id, TASK_NOT_FOUND, "task not found")


    async def send_task(
        self,
        agent: AgentRef,
        message: dict[str, Any],
        *,
        context_id: str | None = None,
        task_id: str | None = None,
        reference_task_ids: list[str] | None = None,
        actor_chain: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Sends `message` to `agent` as a new (or continuing, via `context_id`/`task_id`) A2A
        task and blocks until it finishes, returning the resulting `Task` as wire JSON. This is
        the semantic entry point behind the `SendMessage`/`message/send`/`tasks/send` RPC names."""
        run_id, thread_id, is_live = await self._start_run(
            agent, _params(message, context_id, task_id, reference_task_ids, actor_chain, metadata)
        )
        live = is_live and self._funduq.broker.get(run_id) is not None
        if live:
            events = [item async for item in self._funduq.broker.subscribe(run_id)]
        else:
            events = await self._funduq.get_run_events(run_id)
        stored = await self._funduq.get_run(run_id)
        return build_task(
            run_id,
            thread_id,
            await self._display_name(agent),
            stored.status if stored else "completed",
            events,
        )

    async def send_task_streaming(
        self,
        agent: AgentRef,
        message: dict[str, Any],
        *,
        context_id: str | None = None,
        task_id: str | None = None,
        reference_task_ids: list[str] | None = None,
        actor_chain: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Like `send_task` but yields A2A status/artifact updates as the run progresses instead
        of waiting for completion; behind `SendStreamingMessage`/`message/stream`/
        `tasks/sendSubscribe`. If the run turns out not to be live (e.g. it was already
        finished), yields a single status update reflecting its stored status."""
        run_id, thread_id, is_live = await self._start_run(
            agent, _params(message, context_id, task_id, reference_task_ids, actor_chain, metadata)
        )
        live = is_live and self._funduq.broker.get(run_id) is not None
        events = self._funduq.broker.subscribe(run_id) if live else None

        async def results() -> AsyncIterator[dict[str, Any]]:
            if not live:
                stored = await self._funduq.get_run(run_id)
                status = stored.status if stored else "completed"
                yield status_update_for_run_status(run_id, thread_id, status)
                return
            async for item in events:
                yield agui_event_to_a2a_update(item, run_id, thread_id)
            stored = await self._funduq.get_run(run_id)
            if stored is not None and stored.status != "completed":
                yield status_update_for_run_status(run_id, thread_id, stored.status)

        return results()

    async def resubscribe_task(self, agent: AgentRef, task_id: str) -> AsyncIterator[dict[str, Any]]:
        """Reattaches to an existing task's update stream (`SubscribeToTask`/`tasks/resubscribe`).
        If the task is no longer live, yields its final stored-status update instead of hanging.
        Raises `RunNotFound` if `task_id` doesn't belong to `agent`."""
        run = await self._run_of(agent, task_id)
        thread_id = run.thread_id
        events = self._funduq.broker.subscribe(task_id) if self._funduq.broker.get(task_id) else None

        async def results() -> AsyncIterator[dict[str, Any]]:
            if events is None:
                yield status_update_for_run_status(task_id, thread_id, run.status)
                return
            async for item in events:
                yield agui_event_to_a2a_update(item, task_id, thread_id)
            stored = await self._funduq.get_run(task_id)
            if stored is not None and stored.status != "completed":
                yield status_update_for_run_status(task_id, thread_id, stored.status)

        return results()

    async def get_task(self, agent: AgentRef, task_id: str) -> dict[str, Any]:
        """Returns the current `Task` state for `task_id` as wire JSON. Raises `RunNotFound` if
        `task_id` doesn't belong to `agent`."""
        run = await self._run_of(agent, task_id)
        return build_task(
            task_id,
            run.thread_id,
            await self._display_name(agent),
            run.status,
            await self._funduq.get_run_events(task_id),
        )

    async def cancel_task(self, agent: AgentRef, task_id: str) -> dict[str, Any]:
        """Requests cancellation of the running task and returns its resulting `Task` state
        (funduq can only ask the provider to stop, not force it, so the returned status reflects
        whatever the provider actually did). Raises `RunNotFound` if `task_id` doesn't belong to
        `agent`."""
        run = await self._run_of(agent, task_id)
        self._funduq.cancel_run(task_id)
        current = await self._funduq.get_run(task_id) or run
        return build_task(
            task_id,
            run.thread_id,
            await self._display_name(agent),
            current.status,
            await self._funduq.get_run_events(task_id),
        )


    async def _run_of(self, agent: AgentRef, task_id: str) -> dict[str, Any]:
        """Looks up the run for `task_id`, raising `RunNotFound` if it doesn't exist or belongs
        to a different agent than `agent`."""
        run = await self._funduq.get_run(task_id) if task_id else None
        if run is None or AgentRef(provider_key=run.provider_key, name=run.agent_name) != agent:
            raise RunNotFound(f"no task '{task_id}' for agent '{agent}'")
        return run

    async def _display_name(self, agent: AgentRef) -> str:
        record = await self._funduq.get_agent(agent)
        return record.name if record else agent.name

    async def _start_run(self, agent: AgentRef, params: dict) -> tuple[str, str, bool]:
        """Resolves `params` (a `contextId`/`taskId`/message envelope) to a thread and run,
        creating or reopening whichever is needed, and returns `(run_id, thread_id, is_live)`.
        A message addressed (via `taskId`) to the thread's paused `input-required` task resumes
        it; any other message becomes a new queued run, offered to the provider in arrival
        order — a message sent while a run is active is delivered alongside it, never merged
        or dropped; how the provider sequences its turns is the provider's own business. `is_live` is False
        only if the agent isn't currently served (in which case the run is
        recorded as failed). A `metadata.kyok` opt-in binds the run to the named LLM offering
        (raising `LlmProviderNotFound` for an unknown one), the same road AG-UI callers use.
        Absent that, a `referenceTaskIds` entry both anchors thread lineage to the referenced
        task's thread and, if that task carries a KYOK binding, inherits it for this run."""
        funduq = self._funduq
        async with funduq.session() as session:
            record = await repo.get_agent(session, agent)
            if record is None:
                raise AgentNotFound(f"agent '{agent}' is not registered")

            metadata = params.get("metadata", {})
            kyok = parse_kyok_opt_in(metadata)
            kyok_ref = kyok.llm_provider if kyok is not None else None
            if kyok_ref is not None and await repo.get_llm_provider(session, kyok_ref) is None:
                raise LlmProviderNotFound(f"unknown KYOK LLM provider '{kyok_ref}'")
            metadata = strip_kyok_context(metadata)
            parent_thread_id = await _lineage_parent(session, params)
            context_id = params.get("contextId") or await _context_of_task(session, params.get("taskId"))

            metadata, verified_subject, verified_actors, actor_chain = await verify_caller(
                session, metadata
            )

            thread_id = await repo.ensure_thread(
                session, agent, context_id, parent_thread_id, metadata=metadata
            )

            messages = a2a_message_to_agui_messages(params.get("message", {}))
            run_input = {"thread_id": thread_id, "messages": messages}

            # Two lanes. A message whose taskId names this thread's paused
            # (input-required) task resumes that run with whatever it says —
            # funduq never checks that it answers the question; the provider
            # judges answer-vs-redirect from the thread's shape (the reopen
            # is status-guarded so two concurrent resumes resolve to one;
            # the loser lands in the queue lane like any other utterance).
            # Every other message becomes a new queued run on the thread.
            # taskId carries no further meaning here — v1.0 defines none for
            # a task that isn't asking, and funduq doesn't invent one.
            # Asking to join a turn already in flight is a different verb and
            # an explicit one: the interjection extension
            # (ADDRESSED_RUN_METADATA_KEY, copied into forwardedProps below).
            # Intent is declared by the caller, never inferred from the
            # target's state.
            task_id = params.get("taskId")
            addressed = await repo.get_run(session, task_id) if task_id else None
            reopened = (
                addressed is not None
                and addressed.thread_id == thread_id
                and addressed.status == "input-required"
                and await repo.reopen_run(
                    session,
                    task_id,
                    run_input,
                    metadata=metadata,
                    expected_status="input-required",
                )
            )
            if reopened:
                run_id = task_id
                starting_seq = await repo.get_last_event_seq(session, run_id)
            else:
                await repo.ensure_queue_room(
                    session, thread_id, funduq.settings.thread_queue_limit
                )
                created = await repo.create_run(
                    session, thread_id, agent, "a2a", run_input, metadata=metadata
                )
                run_id = created["run_id"]
                starting_seq = 0

            messages = await repo.append_thread_messages(session, thread_id, run_id, messages)

            if not funduq.is_serving(AgentRef(provider_key=record.provider_key, name=record.name)):
                await funduq.mark_run_status(
                    session, run_id, "failed", metadata={"failureReason": "agent_offline"}
                )
                await session.commit()
                return run_id, thread_id, False

            reference_task_ids = params.get("message", {}).get("referenceTaskIds") or []
            inherited = (
                kyok_ref is None
                and bool(reference_task_ids)
                and funduq.kyok_relay.inherit(reference_task_ids[0], run_id, actor_chain)
            )
            forwarded_props = build_forwarded_props(
                funduq.settings.token_signing_secret,
                run_id,
                agent,
                kyok_ref is not None or inherited,
                None,
                verified_subject,
                verified_actors,
                actor_chain,
                # The extension convention puts the key in the Message's own
                # metadata map; the request-level map is accepted too.
                addressed_run_id=(
                    (params.get("message", {}).get("metadata") or {}).get(
                        ADDRESSED_RUN_METADATA_KEY
                    )
                    or metadata.get(ADDRESSED_RUN_METADATA_KEY)
                ),
            )

            try:
                agui_input = build_run_agent_input(
                    thread_id, run_id, messages, forwarded_props=forwarded_props
                )
            except ValueError as e:
                raise InvalidRunInput(str(e)) from e

            await session.commit()

        if kyok_ref is not None:
            funduq.kyok_relay.bind_run(
                run_id,
                KyokBinding(llm_provider=kyok_ref, context=kyok.context, actor_chain=actor_chain),
            )
        funduq.enqueue_run(run_id, agent, thread_id, agui_input, "a2a", seq=starting_seq)
        return run_id, thread_id, True


def _skills(raw_skills: list[dict[str, Any]]) -> list[pb.AgentSkill]:
    skills = []
    for raw in raw_skills:
        try:
            skills.append(ParseDict(raw, pb.AgentSkill(), ignore_unknown_fields=True))
        except ParseError:
            logger.warning("agent card: skipping a skill that is not an A2A AgentSkill: %r", raw)
    return skills


async def _context_of_task(session, task_id: str | None) -> str | None:
    if not task_id:
        return None
    run = await repo.get_run(session, task_id)
    if run is None:
        raise RunNotFound(f"no task '{task_id}'")
    return run.thread_id


async def _lineage_parent(session, params: dict) -> str | None:
    reference_task_ids = params.get("message", {}).get("referenceTaskIds") or []
    if not reference_task_ids:
        return None
    referenced = await repo.get_run(session, reference_task_ids[0])
    return referenced.thread_id if referenced is not None else None


def _send_args(params: dict[str, Any]) -> dict[str, Any]:
    message = params.get("message", {})
    metadata = params.get("metadata", {}) or {}
    return {
        "message": message,
        "context_id": message.get("contextId") or params.get("contextId"),
        "task_id": message.get("taskId"),
        "reference_task_ids": message.get("referenceTaskIds") or None,
        "actor_chain": metadata.get("actorChain"),
        "metadata": {k: v for k, v in metadata.items() if k != "actorChain"} or None,
    }


def _params(
    message: dict[str, Any],
    context_id: str | None,
    task_id: str | None,
    reference_task_ids: list[str] | None,
    actor_chain: list[str] | None,
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    message = dict(message)
    if reference_task_ids:
        message["referenceTaskIds"] = reference_task_ids
    combined = dict(metadata or {})
    if actor_chain:
        combined["actorChain"] = actor_chain
    return {"message": message, "contextId": context_id, "taskId": task_id, "metadata": combined}


async def _wrap(rpc_id: Any, stream: AsyncIterator[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    async for item in stream:
        yield _result(rpc_id, item)


def _result(rpc_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _error(rpc_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}
