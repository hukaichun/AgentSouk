"""A2A translation: JSON-RPC in, JSON-RPC out.

Extracted from what used to be api_a2a, minus everything about HTTP. The
mapping decisions this holds are the ones worth keeping in core rather than
leaving each integrator to re-derive:

- A2A's `Task.id` **is** souk's `run_id`. There is no separate task concept,
  which is what lets a task id stay valid across however many pause/resume
  rounds a run goes through.
- A2A's `contextId` **is** souk's `thread_id`. It is optional, so a caller
  that omits one gets a fresh thread (the spec-sanctioned first-contact
  case); but one that supplies an unrecognized id is a caller error, not a
  request to create a thread under that name — the opposite of AG-UI's
  caller-minted `threadId`.
- `Message.referenceTaskIds` records lineage only. It is informational in
  A2A's own words, so souk uses it to link a spawned thread back to its
  caller's, and never to group sessions.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from a2a.types import a2a_pb2 as pb
from a2a.utils.constants import PROTOCOL_VERSION_CURRENT, TransportProtocol
from google.protobuf.json_format import ParseDict, ParseError

from souk import repo
from souk.agui import build_run_agent_input
from souk.errors import AgentNotFound, InvalidRunInput, RunNotFound
from souk.identity import verify_actor_chain
from souk.kyok import issue_kyok_token
from souk.models import AgentRef
from souk.pause import is_resuming
from souk.protocols.a2a_translate import (
    a2a_message_to_agui_messages,
    agui_event_to_a2a_update,
    build_task,
    status_update_for_run_status,
    to_wire,
)

if TYPE_CHECKING:
    from souk.core import Souk

logger = logging.getLogger("souk.protocols.a2a")

METHOD_NOT_FOUND = -32601
TASK_NOT_FOUND = -32001

# What the agent card advertises, taken from the SDK rather than typed here:
# it is a claim about which vocabulary the methods below speak, and a claim
# souk got wrong once already by reading shapes out of a module named
# `compat.v0_3` without checking what it was compatibility *for*.
PROTOCOL_VERSION = PROTOCOL_VERSION_CURRENT

# A2A v1.0's JSON-RPC method names are its gRPC service method names, so they
# are read off the service descriptor instead of spelled out. A method souk
# implements that the spec renames now fails this module at import, which is
# the whole reason for taking the dependency.
_A2A_METHODS = {method.name for method in pb.DESCRIPTOR.services_by_name["A2AService"].methods}


def _method(name: str) -> str:
    if name not in _A2A_METHODS:
        raise RuntimeError(
            f"A2AService has no method {name!r} — the spec moved and souk's dispatch is stale. "
            f"It offers: {sorted(_A2A_METHODS)}"
        )
    return name


# Every spelling souk answers to. It emits v1.0 and only v1.0, but a method
# name is free to accept: `message/send` is v0.3's name for SendMessage and
# `tasks/send` was the original, and refusing them buys nothing. The SDK
# itself ships exactly this accommodation (`enable_v0_3_compat` on its own
# dispatcher), so it is the spec's own idea of politeness, not souk's.
SEND = frozenset({_method("SendMessage"), "message/send", "tasks/send"})
STREAM = frozenset({_method("SendStreamingMessage"), "message/stream", "tasks/sendSubscribe"})
GET = frozenset({_method("GetTask"), "tasks/get"})
CANCEL = frozenset({_method("CancelTask"), "tasks/cancel"})
SUBSCRIBE = frozenset({_method("SubscribeToTask"), "tasks/resubscribe"})


@dataclass
class A2AStream:
    """`tasks/sendSubscribe`: a stream of JSON-RPC result envelopes."""

    results: AsyncIterator[dict[str, Any]]

    async def encode(self) -> AsyncIterator[str]:
        """The envelopes as SSE `data:` payloads.

        Encoding lives here rather than in a route so that anyone serving
        souk over their own framework gets the wire format right for free —
        it is part of speaking A2A, not part of speaking HTTP. Framing these
        strings into an actual response stays with whoever owns the server.
        """
        async for item in self.results:
            yield json.dumps(item)


# A2A's own names for its transport bindings, read off the enum so this
# tracks the spec instead of a list souk maintains.
_BINDINGS = frozenset(member.name for member in TransportProtocol)


@dataclass(frozen=True)
class ServedInterface:
    """One way this souk is actually reachable, as a fact supplied to core.

    A2A's `AgentCard` mixes two kinds of field: what an agent *is* (name,
    skills, capabilities, protocol version) and where it can be *reached*
    (`supported_interfaces`, and the auth those interfaces want). The first is
    souk's own data in the protocol's shape, and translating it is exactly
    what protocol adapters are for. The second is a statement about a
    deployment — which transports exist, at which addresses — and core does
    not know whether anyone is serving it at all.

    So the card's interfaces are an input, like a run's input is. This class
    is the fact; building `pb.AgentInterface` from it, with the right enum
    spelling and protocol version, stays core's job, so whoever serves souk
    never hand-writes an A2A field name (see this module's notes on -32601).

    The previous version passed a `public_base_url` and interpolated
    `f"{base}/a2a/id/{agent}/rpc"` — which meant core had decided the route
    layout of every gateway that would ever serve it. Its own docstring said
    core "has no business knowing what souk is called on a network" two lines
    above the line that named the path. Fixing the *value* and keeping the
    *concept* is what left that gap; the concept goes here.

    Verified against the installed `a2a-sdk` rather than assumed: a card with
    no `supported_interfaces` serialises cleanly (the field is `repeated`, and
    proto3 has no required fields), so leaving it to whoever knows the answer
    is not a deviation.
    """

    url: str
    # Which binding this URL speaks, by A2A's own name for it.
    #
    # No default. souk serves A2A over JSON-RPC today and A2A also defines a
    # gRPC binding, so defaulting this would quietly make "JSON-RPC" core's
    # standing assumption about how it is served — which is the assumption
    # this class exists to remove.
    binding: str

    def __post_init__(self) -> None:
        # Checked here rather than when the card is built, because this is
        # constructed once at startup by whoever serves souk: a misspelling
        # fails that process on boot instead of failing a client's card read.
        # The valid set comes from the A2A enum, so it follows the spec rather
        # than a list written down here.
        if self.binding not in _BINDINGS:
            raise ValueError(
                f"unknown A2A binding {self.binding!r} — A2A defines {sorted(_BINDINGS)}"
            )


class A2AAdapter:
    """A2A semantics over a Souk.

    Holds no address of its own: see `ServedInterface` for why the Agent
    Card's `supported_interfaces` is something a caller supplies rather than
    something core builds.

    Takes an `AgentRef` and never a bare display name — see the note under
    `ServedInterface`: the URL a card advertises is the gateway's to shape,
    so it can carry the pair and nothing here has to guess from a name.
    """

    def __init__(self, souk: "Souk") -> None:
        self._souk = souk

    async def agent_card(
        self, agent: AgentRef, interfaces: "list[ServedInterface] | None" = None
    ) -> dict[str, Any]:
        """This agent's card. `interfaces` says where it can be reached, and
        comes from whoever is serving souk — a card built with none simply
        advertises none, which is the truth about a souk nobody is serving."""
        record = await self._souk.get_agent(agent)
        if record is None:
            raise AgentNotFound(f"agent '{agent}' is not registered")
        card = dict(record.agent_card)
        return to_wire(
            pb.AgentCard(
                name=card.get("name", record.name),
                description=card.get("description", ""),
                version="0.1.0",
                # v1.0 replaced the card's single `url` + `preferredTransport`
                # with a list of interfaces, each stating its own binding and
                # protocol version. This is where a client learns to call
                # `SendMessage` rather than probing for a method name and
                # getting -32601 — which is exactly how souk's own drift went
                # unnoticed, since nothing else on the card stated a version.
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
        """The wire rung: a JSON-RPC envelope in, a JSON-RPC envelope out.

        A thin wrapper over the semantic methods below, which is the point —
        the envelope exists for transmission, so anything already in this
        process should call `send_task`/`get_task`/`cancel_task` directly
        rather than constructing `{"jsonrpc": "2.0", ...}` to talk to itself.
        Both rungs run the same A2A semantics, so an in-process caller and a
        remote one are never subtly different.
        """
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
        """RunNotFound is A2A's "task not found" error rather than an
        exception, since a caller asking about an unknown task is an ordinary
        answer, not a failure of the call."""
        try:
            return _result(rpc_id, await coro)
        except RunNotFound:
            return _error(rpc_id, TASK_NOT_FOUND, "task not found")

    # ---- semantic rung: A2A without the envelope

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
        """Run a task to completion and return the resulting A2A Task.

        `context_id` continues an existing conversation; `task_id` continues
        a specific task (its context is looked up, so a caller holding only a
        task id doesn't have to have kept the contextId too);
        `reference_task_ids` records lineage back to the caller's own task;
        `actor_chain` carries caller identity forward (see
        souk.identity.extend_actor_chain — a hop that doesn't extend it is
        where provenance stops).
        """
        run_id, thread_id, is_live = await self._start_run(
            agent, _params(message, context_id, task_id, reference_task_ids, actor_chain, metadata)
        )
        live = is_live and self._souk.broker.get(run_id) is not None
        if live:
            # No cleanup on early exit, deliberately: a caller disconnecting
            # mid-wait does not cancel the run.
            events = [item async for item in self._souk.broker.subscribe(run_id)]
        else:
            # Nothing live to wait on — already paused/finished, already
            # failed fast, or a duplicate call racing a live run. Report its
            # current persisted state instead.
            events = await self._souk.get_run_events(run_id)
        stored = await self._souk.get_run(run_id)
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
        """Same as send_task, but yields A2A status/artifact updates as they
        arrive instead of waiting for the task to finish."""
        run_id, thread_id, is_live = await self._start_run(
            agent, _params(message, context_id, task_id, reference_task_ids, actor_chain, metadata)
        )
        live = is_live and self._souk.broker.get(run_id) is not None
        # Subscribed before `results` is iterated, for the same reason as
        # AGUIAdapter's relay: a short run can finish before the caller
        # starts reading, and a subscription taken then would be empty.
        events = self._souk.broker.subscribe(run_id) if live else None

        async def results() -> AsyncIterator[dict[str, Any]]:
            if not live:
                # Same "nothing live" situation as tasks/send, but streaming:
                # one status update reflecting the current persisted state,
                # then close.
                stored = await self._souk.get_run(run_id)
                status = stored.status if stored else "completed"
                yield status_update_for_run_status(run_id, thread_id, status)
                return
            async for item in events:
                yield agui_event_to_a2a_update(item, run_id, thread_id)
            # Corrects the record: the loop above already sent whatever the
            # raw RUN_FINISHED translated to (always "completed", see
            # agui_event_to_a2a_update) — this overrides it with the real
            # persisted outcome, so a live watcher isn't left with a false
            # "completed" as the last word.
            stored = await self._souk.get_run(run_id)
            if stored is not None and stored.status != "completed":
                yield status_update_for_run_status(run_id, thread_id, stored.status)

        return results()

    async def resubscribe_task(self, agent: AgentRef, task_id: str) -> AsyncIterator[dict[str, Any]]:
        """`tasks/resubscribe`: rejoin a task's stream after losing the
        connection it was started on.

        Only what happens *from now on*: the spec's own framing is resuming a
        stream, and souk has `tasks/get` for the whole story so far — a
        reconnecting caller that also wants the backlog asks for it. A task
        that is no longer live gets one final status update rather than an
        empty stream, so a caller reconnecting a moment too late still learns
        the outcome instead of watching nothing.
        """
        run = await self._run_of(agent, task_id)
        thread_id = run.thread_id
        events = self._souk.broker.subscribe(task_id) if self._souk.broker.get(task_id) else None

        async def results() -> AsyncIterator[dict[str, Any]]:
            if events is None:
                yield status_update_for_run_status(task_id, thread_id, run.status)
                return
            async for item in events:
                yield agui_event_to_a2a_update(item, task_id, thread_id)
            stored = await self._souk.get_run(task_id)
            if stored is not None and stored.status != "completed":
                yield status_update_for_run_status(task_id, thread_id, stored.status)

        return results()

    async def get_task(self, agent: AgentRef, task_id: str) -> dict[str, Any]:
        """A task's current state. `task_id` is a run_id — A2A's Task.id is
        not a separate concept. Scoped to agent, so a request against one
        agent's endpoint can't read another agent's run."""
        run = await self._run_of(agent, task_id)
        return build_task(
            task_id,
            run.thread_id,
            await self._display_name(agent),
            run.status,
            await self._souk.get_run_events(task_id),
        )

    async def cancel_task(self, agent: AgentRef, task_id: str) -> dict[str, Any]:
        """Requests cancellation and reports the run's *real* state.

        Deliberately not hardcoded to "canceled": souk asks a provider to
        stop, it cannot make it stop, and a provider is free to ignore the
        request and finish normally. Claiming the task was cancelled here
        would tell the caller something souk has not verified — and could be
        flatly contradicted by the run's own next status. What is honest is
        that the request was made; the resulting state is whatever the run
        actually reports (typically `cancelling` while the provider is still
        winding down).
        """
        run = await self._run_of(agent, task_id)
        self._souk.cancel_run(task_id)
        # Re-read: the request may already have settled the run (nothing had
        # claimed it), or moved it to `cancelling`.
        current = await self._souk.get_run(task_id) or run
        return build_task(
            task_id,
            run.thread_id,
            await self._display_name(agent),
            current.status,
            await self._souk.get_run_events(task_id),
        )

    # ---- internals

    async def _run_of(self, agent: AgentRef, task_id: str) -> dict[str, Any]:
        run = await self._souk.get_run(task_id) if task_id else None
        if run is None or AgentRef(provider_key=run.provider_key, name=run.agent_name) != agent:
            raise RunNotFound(f"no task '{task_id}' for agent '{agent}'")
        return run

    async def _display_name(self, agent: AgentRef) -> str:
        record = await self._souk.get_agent(agent)
        return record.name if record else agent.name

    async def _start_run(self, agent: AgentRef, params: dict) -> tuple[str, str, bool]:
        """Queues a run from tasks/send(Subscribe) params.

        Returns (run_id, thread_id, is_live). `is_live=False` means nothing
        was enqueued — this session already had an active run, or the agent
        was already known offline and the run was created pre-failed — so
        the caller gets a run whose persisted state is authoritative rather
        than something to wait on. That is also what makes a repeated
        tasks/send on the same session idempotent (same run_id back, current
        real state) instead of forking a second concurrent run.

        `params.id`, if the caller sent one, is accepted as part of the
        JSON-RPC shape and ignored: souk mints run ids.
        """
        souk = self._souk
        async with souk.session() as session:
            record = await repo.get_agent(session, agent)
            if record is None:
                raise AgentNotFound(f"agent '{agent}' is not registered")

            metadata = params.get("metadata", {})
            # A KYOK caller context arriving here — whether misdirected or
            # from some future A2A opt-in — must not reach the database:
            # run metadata comes back verbatim through the unauthenticated
            # thread endpoints (see protocols.agui's _split_kyok for the
            # full argument). Stripped unconditionally, acted on never —
            # KYOK binding on the A2A path is inheritance-only today.
            if isinstance(metadata.get("kyok"), dict) and "context" in metadata["kyok"]:
                metadata = {
                    **metadata,
                    "kyok": {k: v for k, v in metadata["kyok"].items() if k != "context"},
                }
            parent_thread_id = await _lineage_parent(session, params)
            context_id = params.get("contextId") or await _context_of_task(session, params.get("taskId"))

            # Opt-in caller identity, same mechanism as AG-UI's: unsigned
            # calls are allowed, but a chain that is present and fails to
            # verify is rejected rather than silently treated as anonymous —
            # that is more likely tampering than a caller choosing not to
            # send one.
            verified_subject = None
            verified_actors: list[dict] = []
            actor_chain = metadata.get("actorChain")
            if actor_chain:
                result = verify_actor_chain(actor_chain)
                verified_subject = result.subject
                for public_key in result.actor_public_keys:
                    resolved = await repo.get_agent_name_for_public_key(session, public_key)
                    verified_actors.append({"publicKey": public_key, "agentName": resolved})
                metadata = {
                    **metadata,
                    "verifiedActorChain": {"subject": verified_subject, "actors": verified_actors},
                }

            # create_if_missing stays False here, unlike AG-UI: `contextId`
            # is optional, so omitting it still yields a fresh thread, but
            # supplying an unrecognized one is a caller error (ThreadNotFound).
            thread_id = await repo.ensure_thread(
                session, agent, context_id, parent_thread_id, metadata=metadata
            )

            active = await repo.get_active_run_for_thread(session, thread_id)
            # A2A never carries a resume, so is_resuming(active, None) is
            # always False: a paused run reached over A2A can't be bypassed
            # here. Resolving it happens on the same thread over the agent's
            # AG-UI endpoint — see souk/pause.py.
            if active is not None and not is_resuming(active, None):
                return active["run_id"], thread_id, False
            resuming_run_id = active["run_id"] if active is not None else None

            messages = a2a_message_to_agui_messages(params.get("message", {}))
            run_input = {"thread_id": thread_id, "messages": messages}

            if resuming_run_id is not None:
                run_id = resuming_run_id
                starting_seq = await repo.get_last_event_seq(session, run_id)
                await repo.reopen_run(session, run_id, run_input, metadata=metadata)
            else:
                created = await repo.create_run(
                    session, thread_id, agent, "a2a", run_input, metadata=metadata
                )
                run_id = created["run_id"]
                starting_seq = 0

            messages = await repo.append_thread_messages(session, thread_id, run_id, messages)

            if not souk.is_serving(AgentRef(provider_key=record.provider_key, name=record.name)):
                await souk.mark_run_status(
                    session, run_id, "failed", metadata={"failureReason": "agent_offline"}
                )
                await session.commit()
                return run_id, thread_id, False

            # The raw chain travels too, not just the resolved summary: a
            # provider that delegates further needs the actual prior JWTs to
            # extend it.
            forwarded_props: dict[str, Any] | None = (
                {"caller": {"subject": verified_subject, "actors": verified_actors, "chain": actor_chain}}
                if verified_subject is not None
                else None
            )

            # KYOK inheritance: a delegating run's binding — same offering,
            # same caller context — carries to the run it spawns, copied by
            # souk itself so the context never transits the delegating
            # agent's hands (an agent forwarding `metadata.kyok` would be
            # an agent holding the caller's credential: the session-id
            # disclosure with a new face). The child's binding gets the
            # chain that reached *this* run, verified above, which is what
            # lets the LLM provider check the delegation path before
            # spending the caller's budget on a sub-agent it never heard
            # of. "One-time context" thereby means one authorized run
            # tree; policing the tree's shape is the LLM provider's job,
            # from the chain.
            reference_task_ids = params.get("message", {}).get("referenceTaskIds") or []
            inherited = bool(reference_task_ids) and souk.kyok_relay.inherit(
                reference_task_ids[0], run_id, actor_chain
            )
            if inherited:
                forwarded_props = {
                    **(forwarded_props or {}),
                    "kyok": {
                        "token": issue_kyok_token(
                            run_id, agent, souk.settings.token_signing_secret
                        )
                    },
                }

            try:
                agui_input = build_run_agent_input(
                    thread_id, run_id, messages, forwarded_props=forwarded_props
                )
            except ValueError as e:
                raise InvalidRunInput(str(e)) from e

            await session.commit()

        souk.enqueue_run(run_id, agent, thread_id, agui_input, "a2a", seq=starting_seq)
        return run_id, thread_id, True


def _skills(raw_skills: list[dict[str, Any]]) -> list[pb.AgentSkill]:
    """A provider registers skills as free-form dicts (see the registration
    model), so they are put through A2A's own `AgentSkill` before souk
    advertises them — a card souk publishes should be a card, not whatever
    shape a provider happened to send.

    Unknown keys are dropped rather than rejected: a provider carrying its own
    extra fields is not a reason to refuse to serve its card, and there is
    nowhere in `AgentSkill` to keep them. One unparseable skill is skipped,
    not fatal, for the same reason.
    """
    skills = []
    for raw in raw_skills:
        try:
            skills.append(ParseDict(raw, pb.AgentSkill(), ignore_unknown_fields=True))
        except ParseError:
            logger.warning("agent card: skipping a skill that is not an A2A AgentSkill: %r", raw)
    return skills


async def _context_of_task(session, task_id: str | None) -> str | None:
    """`Message.taskId` — the current spec's way to say "this message
    continues that task". A2A's Task.id *is* souk's run_id, so the task's
    context is simply its run's thread.

    Unlike `referenceTaskIds` (informational, so an unknown id is ignored),
    this one is a claim about where the message belongs: an id souk doesn't
    know is a caller error, and quietly opening a fresh thread instead would
    strand the conversation the caller thought it was continuing.
    """
    if not task_id:
        return None
    run = await repo.get_run(session, task_id)
    if run is None:
        raise RunNotFound(f"no task '{task_id}'")
    return run.thread_id


async def _lineage_parent(session, params: dict) -> str | None:
    """Real A2A `Message.referenceTaskIds` — "other task IDs this message
    references for additional context" — not a souk invention. A caller
    delegating to a sub-agent can reference its own current task id, letting
    souk link the spawned thread back to the caller's for lineage.

    Only the first entry is used, and an id souk doesn't recognize is treated
    exactly like not sending one: this is informational context, not a claim
    souk verifies.
    """
    reference_task_ids = params.get("message", {}).get("referenceTaskIds") or []
    if not reference_task_ids:
        return None
    referenced = await repo.get_run(session, reference_task_ids[0])
    return referenced.thread_id if referenced is not None else None


def _send_args(params: dict[str, Any]) -> dict[str, Any]:
    """JSON-RPC params to the semantic methods' keyword arguments.

    `contextId` and `taskId` live on the *message* in the current spec
    (MessageSendParams is `{message, configuration?, metadata?}` — nothing
    else). souk's first A2A implementation read `contextId` from the top
    level and took a caller-assigned `id` there too, so both are still
    read, message first.

    A caller-assigned task id remains ignored as an *identifier* — souk mints
    those — but `taskId` naming an existing task is not the same thing: that
    is a caller continuing a task, and it is honoured by resolving the task's
    thread (see `_start_run`).
    """
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
    """The inverse: semantic arguments back into the params shape _start_run
    reads. One shape, so both rungs go through identical handling."""
    message = dict(message)
    if reference_task_ids:
        message["referenceTaskIds"] = reference_task_ids
    combined = dict(metadata or {})
    if actor_chain:
        combined["actorChain"] = actor_chain
    return {"message": message, "contextId": context_id, "taskId": task_id, "metadata": combined}


async def _wrap(rpc_id: Any, stream: AsyncIterator[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    """Put each semantic update into a JSON-RPC envelope for the wire."""
    async for item in stream:
        yield _result(rpc_id, item)


def _result(rpc_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _error(rpc_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}
