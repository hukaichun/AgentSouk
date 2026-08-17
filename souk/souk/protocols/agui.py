"""AG-UI translation: a RunAgentInput in, a stream of AG-UI events out.

Extracted from what used to be protocols.agui's AGUIAdapter.run, minus everything that was
about HTTP. It yields AG-UI event mappings; turning those into SSE frames,
and these errors into status codes, belongs to whoever serves it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ag_ui.core import RunAgentInput

from souk import repo
from souk.models import AgentRef, LlmRef
from souk.agui import build_run_agent_input, rewrite_message_ids
from souk.errors import AgentNotFound, InvalidRunInput
from souk.identity import verify_actor_chain
from souk.kyok import KyokBinding, issue_kyok_token
from souk.pause import is_resuming

if TYPE_CHECKING:
    from souk.core import Souk


@dataclass
class EventStream:
    """A run is live; `events` yields its AG-UI events."""

    thread_id: str
    run_id: str
    events: AsyncIterator[dict[str, Any]]

    async def encode(self) -> AsyncIterator[str]:
        """The events as SSE `data:` payloads.

        Encoding lives here rather than in a route so that anyone serving
        souk over their own framework gets the wire format right for free —
        it is part of speaking AG-UI, not part of speaking HTTP. Framing
        these strings into an actual response stays with whoever owns the
        server.
        """
        async for event in self.events:
            yield json.dumps(event)


@dataclass
class ThreadSnapshot:
    """The thread already had an active run, so this call started nothing.

    Rather than erroring or quietly queueing a duplicate, souk hands back the
    thread's current state — including the pending run's real status — so the
    caller can act on it. This doubles as the catch-up path for a run that has
    since paused.
    """

    data: dict[str, Any]


class AGUIAdapter:
    """AG-UI semantics over a Souk. Construct one per souk; it holds no
    per-request state.

    Takes an `AgentRef` and never a bare display name: names are not
    exclusive across identities, so a name is not an address. AG-UI's own
    request body has no agent field at all — which agent this is comes from
    wherever the gateway put it, and the gateway is what knows the pair.
    """

    def __init__(self, souk: "Souk") -> None:
        self._souk = souk

    async def run(self, agent: AgentRef, body: RunAgentInput) -> EventStream | ThreadSnapshot:
        """Start a run for `agent` from a real `ag_ui.core.RunAgentInput`.

        `body.run_id`, whatever the caller sent, is never used — souk always
        mints its own; the field is only present because AG-UI's schema
        requires it.
        """
        souk = self._souk
        async with souk.session() as session:
            # An existence check and nothing more — deliberately not rebound
            # to the record it returns. Everything below takes the `AgentRef`
            # this was called with; an `AgentRecord` is a different type that
            # merely happens to carry the same two fields.
            if await repo.get_agent(session, agent) is None:
                raise AgentNotFound(f"agent '{agent}' is not registered")

            # Not a declared field on ag_ui.core.RunAgentInput (extra="allow"
            # — see that model), so it's absent rather than defaulted when
            # the caller doesn't send one.
            metadata = getattr(body, "metadata", None) or {}
            resume = [r.model_dump(mode="json", by_alias=True) for r in body.resume] if body.resume else None

            metadata, verified_subject, verified_actors, actor_chain = await _verify_caller(
                session, metadata
            )

            # KYOK opt-in: the caller names, per run, which registered LLM
            # offering — the (providerKey, name) pair, because names are
            # not exclusive across identities — answers this run's
            # completion calls, checked against the durable roster here so
            # a typo fails the run at start instead of surfacing as a 503
            # on the provider's first LLM call. Whether that offering is
            # *attached* is deliberately not checked — reachability is a
            # per-call fact (see protocols.kyok), same as agent liveness
            # is for runs.
            #
            # `metadata.kyok.context` — the caller's credential *to the
            # LLM provider* — is split out before metadata goes anywhere
            # near the database: run metadata comes back verbatim through
            # the unauthenticated thread endpoints and the agent provider
            # holds a thread_id, so persisting it would hand the one party
            # KYOK defends against the caller's credential. It lives only
            # in the run's in-memory binding, and dies with the run.
            metadata, kyok_ref, kyok_context = _split_kyok(metadata)
            if kyok_ref is not None:
                if await repo.get_llm_provider(session, kyok_ref) is None:
                    raise InvalidRunInput(f"unknown KYOK LLM provider '{kyok_ref}'")

            # AG-UI's `threadId` is minted by the *caller* (the schema
            # requires it) and AG-UI has no separate "create thread" concept,
            # so an id souk hasn't seen is indistinguishable from "this is a
            # brand new conversation", not a caller error. create_if_missing
            # mints a real, souk-generated thread_id in that case rather than
            # 404ing — see souk-no-forced-protocol-deviation: a standard
            # AG-UI client that has never heard of `POST /threads` must work.
            thread_id = await repo.ensure_thread(
                session, agent, body.thread_id, metadata=metadata, create_if_missing=True
            )

            # A thread only ever has one active run at a time — a second
            # concurrent one would fork its otherwise-linear history with no
            # clean way to merge it back. (A freshly-minted thread_id has no
            # active run, so this is a no-op for it.)
            active = await repo.get_active_run_for_thread(session, thread_id)
            if active is not None and not is_resuming(active, resume):
                return ThreadSnapshot(await repo.get_thread_snapshot(session, thread_id))
            resuming_run_id = active["run_id"] if active is not None else None

            input_dump = body.model_dump(mode="json", by_alias=True)
            # The caller's whole request body is persisted as the run's
            # input, and the body carries metadata too — so the context has
            # a second road into the database, and it gets the same strip.
            # Found by the leak probe in test_llm_provider_drives_kyok, not
            # by reading; keep that test if you touch this.
            if isinstance(input_dump.get("metadata"), dict):
                input_dump["metadata"], _, _ = _split_kyok(input_dump["metadata"])
            if resuming_run_id is not None:
                # Reopens the *same* run_id for another round rather than
                # minting a new one — a stable identity across pause/resume
                # rounds is what lets this run's own A2A Task.id, if it has
                # one, stay valid without ever being retargeted.
                run_id = resuming_run_id
                starting_seq = await repo.get_last_event_seq(session, run_id)
                await repo.reopen_run(session, run_id, input_dump, metadata=metadata)
            else:
                created = await repo.create_run(
                    session, thread_id, agent, "ag-ui", input_dump, metadata=metadata
                )
                run_id = created["run_id"]
                starting_seq = 0

            # append_thread_messages assigns each message its real
            # souk-minted id (discarding whatever the caller sent) and hands
            # back the same messages with `id` set to it — this return value,
            # not body.messages, is what goes to the provider.
            raw_messages = [m.model_dump(mode="json", by_alias=True) for m in body.messages]
            messages = await repo.append_thread_messages(session, thread_id, run_id, raw_messages)

            # Fast-fail (souk.health's queued-timeout sweep covers the race
            # where the target goes offline *after* this check): if souk
            # already knows the target is offline, don't queue at all — emit
            # a terminal event and close, instead of opening a stream that
            # would sit idle until the broker gave up on it.
            if not souk.is_serving(agent):
                await souk.mark_run_status(
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
                        souk.settings.token_signing_secret,
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

        # Bound before the run can produce its first event, and by
        # offering, not connection: the binding survives the LLM provider
        # reconnecting, and dies with the run through the broker's forget
        # funnel. The caller's context and this run's verified chain ride
        # in the binding — souk-internal, never persisted.
        if kyok_ref is not None:
            souk.kyok_relay.bind_run(
                run_id,
                KyokBinding(llm_provider=kyok_ref, context=kyok_context, actor_chain=actor_chain),
            )
        souk.enqueue_run(run_id, agent, thread_id, input_json, "ag-ui", seq=starting_seq)
        # Subscribed here, not inside _relay: an async generator's body does
        # not run until it is first iterated, and a run that finishes before
        # the caller starts reading would have nothing left to subscribe to.
        return EventStream(thread_id, run_id, _relay(souk.broker.subscribe(run_id)))


def _split_kyok(metadata: dict) -> tuple[dict, LlmRef | None, Any]:
    """Reads the KYOK opt-in out of metadata and returns metadata with the
    caller's context removed — the context must never reach anything that
    persists (see the caller in `run`), and taking it out here means no
    later line has to remember to.

    `metadata.kyok.llmProvider` is `{"providerKey": ..., "name": ...}` —
    the pair, because a bare name is not an address (two providers both
    offering `gpt4` is normal). Malformed is treated as absent rather than
    an error, because metadata is free-form by contract; only a well-formed
    opt-in opts in.
    """
    kyok = metadata.get("kyok")
    if not isinstance(kyok, dict):
        return metadata, None, None
    context = kyok.get("context")
    if "context" in kyok:
        metadata = {**metadata, "kyok": {k: v for k, v in kyok.items() if k != "context"}}
    target = kyok.get("llmProvider")
    if not isinstance(target, dict):
        return metadata, None, context
    provider_key, name = target.get("providerKey"), target.get("name")
    if not (isinstance(provider_key, str) and provider_key and isinstance(name, str) and name):
        return metadata, None, context
    return metadata, LlmRef(provider_key=provider_key, name=name), context


async def _relay(events: AsyncIterator[Any]) -> AsyncIterator[dict[str, Any]]:
    """The run's events, with provider-generated message ids remapped to
    souk-assigned ones consistently across a message's START/CONTENT/END
    (see souk.agui.rewrite_message_ids).

    No `finally`, deliberately: this loop ending some other way than
    the stream's natural end — the caller disconnected, or this generator was
    closed — does not cancel the run. souk's own state is authoritative and
    the run keeps going whether or not anyone is still watching this
    particular stream; a dropped connection shouldn't throw away in-progress
    work a reconnect could catch up on. Cancelling is an explicit act only.
    """
    message_id_map: dict[str, str] = {}
    async for item in events:
        yield rewrite_message_ids(item, message_id_map)


async def _offline_events(thread_id: str, run_id: str) -> AsyncIterator[dict[str, Any]]:
    """A real run always announces its own ids via RUN_STARTED first (every
    compliant AG-UI provider copies them from the RunAgentInput it was
    given). This path never reaches a provider, so it synthesizes the same
    standard, in-band announcement rather than relying on a souk-invented
    header — RunErrorEvent's own schema has no thread_id/run_id field to
    carry them otherwise."""
    yield {"type": "RUN_STARTED", "threadId": thread_id, "runId": run_id}
    yield {"type": "RUN_ERROR", "message": "agent is currently offline"}


async def _verify_caller(session, metadata: dict) -> tuple[dict, Any, list[dict], Any]:
    """Optional, opt-in caller identity: `metadata.actorChain` is an ordered
    list of compact JWTs (see souk.identity.verify_actor_chain). Unsigned
    calls are still allowed; a chain that is present but fails to verify is
    rejected outright rather than silently treated as anonymous, since that
    is more likely tampering than a caller choosing not to send one.
    """
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


def build_forwarded_props(
    signing_secret: str,
    run_id: str,
    agent: AgentRef,
    kyok_enabled: bool,
    caller_forwarded_props: Any,
    verified_subject: Any = None,
    verified_actors: list[dict] | None = None,
    actor_chain: Any = None,
) -> Any:
    """souk's own additions to `forwardedProps`, merged into whatever the
    caller already supplied (its own app-specific context is real AG-UI usage
    too, so souk must not clobber it).

    A KYOK token names this run; the LLM provider answering its calls is
    the one the caller bound the run to, never anything in the token. A
    provider that doesn't look for `forwardedProps.kyok` simply never sees
    it and calls its own configured LLM as always — KYOK is opt-in on both
    sides independently, and souk forces neither.

    Deliberately just the token, not a baseUrl too: the URL external callers
    use to reach souk is often not reachable from inside a provider's own
    network (see docker-compose.yml, where providers reach souk at
    `http://souk:8000`). A provider already knows how it reaches souk — it is
    the same URL it registers and polls against.
    """
    extra: dict[str, Any] = {}
    if kyok_enabled:
        extra["kyok"] = {"token": issue_kyok_token(run_id, agent, signing_secret)}
    if verified_subject is not None:
        # The raw chain travels too, not just the resolved summary: a
        # provider that wants to delegate further needs the actual prior JWTs
        # to extend the chain, not souk's readable description of it.
        extra["caller"] = {
            "subject": verified_subject,
            "actors": verified_actors or [],
            "chain": actor_chain,
        }
    if not extra:
        return caller_forwarded_props
    if isinstance(caller_forwarded_props, dict):
        return {**caller_forwarded_props, **extra}
    return extra
