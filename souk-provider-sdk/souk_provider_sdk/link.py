"""One provider joined to one souk — both directions, one object.

Work arrives and results go back, and those are not two relationships. Over a
wire they are literally one socket: run frames down, event frames up. In
process they are one souk and one runtime. So they are one object, with the
two things that actually vary per transport left abstract.

    souk ──deliver()──▶ │        │ ──offer()──▶ runtime ──▶ agent
                        │  Link  │
    souk ◀─report_event─│        │ ◀────────── runtime's output loop

This used to be `SoukConnection`, covering only the downward half, with the
upward half as two loose callables on `ProviderRuntime`. That split was
accidental rather than designed: the in-process implementation always did
both — it held the souk *and* the runtime and wired the callbacks itself —
so naming one direction and not the other left the object half-described and
left nowhere to put a third thing (see `thread_messages` below).

**A gateway's `SocketProvider` is not one of these.** An earlier version of
this file said it was. It is not: it lives on souk's side, holds an outbound
queue and no runtime, and satisfies `souk.broker.ConnectedProvider` because
that is souk's own protocol — not by inheriting anything here, which it could
not do and should not. The provider-side object opposite it is the socket
*client*, which does carry both directions over its one connection and is
what would subclass this.

Nothing here imports souk. Every souk name is reached by attribute or by
call, which is what keeps this package's dependency list `cryptography` +
`pyjwt` + `ag-ui-protocol` — see `contract.py`. That last one is not souk:
it is the schema `thread_messages` hands back, the same package souk itself
validates every message against before it is ever stored.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ag_ui.core import Message, RunAgentInput

from souk_provider_sdk.provider import DeliveredRun


class SoukLink(ABC):
    """The souk this provider is attached to, and this provider as souk sees
    it. Subclass per transport.

    Souk's broker calls `public_key`, `max_concurrent_runs`, `deliver` and
    `cancel` — its `ConnectedProvider` protocol, satisfied structurally.
    Souk type-checks nothing here and could not: it has to accept a provider
    that never imported this package.

    The reporting half is called by `ProviderRuntime`'s output loop, from its
    own task and in production order.
    """

    # ---- souk → provider

    @property
    @abstractmethod
    def public_key(self) -> str:
        """This provider's Ed25519 public key.

        Established when it connected, not per run: every event it later
        reports is checked against it, because holding a connection is not
        the same as holding a particular run.
        """

    @property
    @abstractmethod
    def max_concurrent_runs(self) -> int | None:
        """How many runs this provider will have going at once, across every
        agent it serves. `None` is unlimited.

        souk keeps a bucket this size and offers nothing once it is empty, so
        a link that omits it is one souk will either starve or overrun. It is
        declared rather than inferred, because souk cannot see inside a
        provider.
        """

    async def deliver(self, run: Any) -> bool:
        """souk is offering this run. Take it, or say no.

        Concrete, and the only place souk's field names appear: `run` is
        souk's `ClaimedRun`, and what reaches `offer` is a `DeliveredRun`.
        Override `offer`, not this.

        True is the ack and means the run has started. Anything else — False,
        or an exception — leaves it queued to be offered again, which is how
        a full provider says so.
        """
        return await self.offer(
            DeliveredRun(
                run_id=run.run_id,
                agent_name=run.agent.name,
                run_input=RunAgentInput.model_validate(run.run_input),
                thread_id=run.thread_id,
            )
        )

    @abstractmethod
    async def offer(self, run: DeliveredRun) -> bool:
        """Carry this run to whoever will execute it, and answer whether it
        was taken. A direct call, a frame and an ack — this is one of the two
        things a transport has to decide."""

    @abstractmethod
    def cancel(self, run_id: str) -> None:
        """souk is asking for a run to stop.

        A request, not an instruction: souk publishes it and then waits to
        see what the run's stream does. One that ignores it and finishes has
        finished, and that is what souk records. Synchronous and returns
        nothing, because there is no answer worth waiting for.
        """

    # ---- provider → souk
    #
    # Async, like everything else here. These were synchronous, on a rule
    # that turned out to describe a different line: "an agent must never wait
    # on what is downstream" is true and is what `ProviderRuntime`'s output
    # *queue* provides — the agent does `put_nowait` and is gone, whatever
    # the drain does afterwards. The other half, "the end marker is sent
    # while unwinding a cancellation where an await would be interrupted",
    # is also true and also about the queue put in `_execute`'s `finally`;
    # `finish_run` is called later, from the drain's own task, which is not
    # being cancelled.
    #
    # Awaiting matters for a transport with a real wire under it: a socket
    # link can await its write, and backpressure lands on the one output
    # queue. Synchronous, every such transport has to build a second queue
    # of its own and return immediately — the same mechanism, once per
    # transport, with nowhere for the pressure to go.

    @abstractmethod
    async def report_event(self, run_id: str, event: Any) -> None:
        """One event this run's agent produced."""

    @abstractmethod
    async def finish_run(self, run_id: str) -> None:
        """This run's agent stream has ended.

        souk decides the outcome from what it saw, so this asserts nothing
        about success — only that there is nothing more coming.
        """

    @abstractmethod
    async def thread_messages(
        self, thread_id: str, *, limit: int | None = None
    ) -> list[Message]:
        """This thread's accumulated messages, oldest first.

        The one thing a provider cannot work out for itself. What arrives in
        `run_input` is exactly what the *caller* sent for this run and nothing
        more, so an agent reached over AG-UI may see ten turns while the same
        agent reached over A2A sees one message — `message/send` carries one.
        The provider cannot tell those apart, and receiving a single message
        is indistinguishable from a fresh conversation.

        Deliberately a query rather than something added to `run_input`:
        which context an agent wants is the agent's business. Windows differ,
        costs differ, some agents summarise instead. souk holds the history
        and does not decide how much of it anyone needs.

        **Overlaps `run_input["messages"]` on purpose.** This returns the
        thread, including the messages this run was started with, rather than
        "everything before this run" — that second definition has no clean
        meaning across a pause and resume, where a run is reopened and
        appended to. Every message carries souk's own id, so a caller that
        wants the difference can take it.

        `limit` keeps the most recent N, because context is wanted from the
        recent end. It is here from the first version rather than added
        later: over a wire this is one response frame, a thread months old is
        an unbounded one, and bounding it afterwards is a wire change.

        Typed as `ag_ui.core.Message`, not a bare dict. Every message here was
        built from that same type on souk's side — its own AG-UI turns
        dumped straight from `RunAgentInput`, and A2A turns translated into
        the same shape before storage (see `a2a_translate.py`) — so a dict
        would only be throwing that array away and asking every provider to
        reassert it by hand. `InProcessLink` validates against it too, so a
        row that ever drifted from the shape fails loudly here instead of at
        whatever field access a provider happened to try first.
        """
