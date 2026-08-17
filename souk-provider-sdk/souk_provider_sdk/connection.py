"""What souk sees when it looks at a provider, whatever is underneath.

souk's broker knows a provider by four things — who it is, how much it will
take at once, how to hand it a run, how to ask it to stop one — and nothing
about what carries them. `souk.broker.ConnectedProvider` is that protocol on
souk's side. This is the base class on the provider's side of it, and the
whole reason it exists is the one step every transport would otherwise
repeat:

    souk's ClaimedRun ──▶ DeliveredRun ──▶ however this transport carries it

`deliver` is concrete and does that translation once. Every field name souk
owns that this package depends on is in that one method, for every transport
there will ever be — which is the point. Written per-transport instead, it
is four field names copied per binding, and copies drift: the loop used to
read souk's object directly, souk handed over a dispatch object whose input
field is `input_json`, and the first real provider died on its first run.

Subclasses say only what is actually different: what to do with a
`DeliveredRun`, and how to ask for a stop.

    class InProcessProvider(SoukConnection):   # a direct call
    class SocketProvider(SoukConnection):      # a frame, and an ack to wait on

**Only the souk-facing half is here.** Reporting events back is not, because
it is not always the same object's job: in-process the runtime is right
there and its callbacks go straight to souk, but over a wire the connection
souk talks to lives in the gateway and the runtime is on the far side of the
socket. A base class covering both directions would fit exactly one of them.

souk is never imported. Every souk name here is reached by attribute, which
is what lets this package keep `cryptography` + `pyjwt` as its whole
dependency list.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from souk_provider_sdk.provider import DeliveredRun


class SoukConnection(ABC):
    """One provider, as souk's broker sees it.

    Satisfies `souk.broker.ConnectedProvider` structurally — souk type-checks
    nothing here, and could not: it must be able to accept a provider that
    never imported this package.
    """

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
        """How many runs it will have going at once, across every agent it
        serves. `None` is unlimited.

        souk keeps a bucket this size and offers nothing once it is empty, so
        a connection that forgets to report it is one souk will either starve
        or overrun. It is declared beside the calls rather than inferred,
        because souk cannot see inside a provider.
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
                run_input=run.run_input,
                thread_id=run.thread_id,
            )
        )

    @abstractmethod
    async def offer(self, run: DeliveredRun) -> bool:
        """Carry this run to whoever will execute it, and answer whether it
        was taken. A direct call, a frame and an ack, a write to a queue —
        this is the only thing a transport has to decide."""

    @abstractmethod
    def cancel(self, run_id: str) -> None:
        """souk is asking for a run to stop.

        A request, not an instruction: souk publishes it and then waits to
        see what the run's stream does. One that ignores it and finishes has
        finished, and that is what souk records. Synchronous and returns
        nothing, because there is no answer worth waiting for.
        """
