"""The loop that drives a provider: claim runs, push their events back.

A provider says *what* it can run (see souk/providers.py). This says *how it
gets work*, and it is the same loop whether the provider shares souk's
process or sits behind a wire — `souk_agent_sdk.client` runs it on the far
side of one, and which wire that is has no bearing on anything here:

    while True:
        runs = await souk.claim_work(token, agent_names, max_claim=capacity)
        for run in runs:
            spawn(execute(run))          # events reported back per run

Which is the inversion. Core used to *call* a provider and pull a generator
per run, which cost an extra queue and an extra routing table on every event
and left an in-process provider with no way to say "two at a time" — the
remote side pulled to claim, the in-process side only ever got pushed at. See
docs/library-architecture.md for the measurements.

Claiming and concurrency belong to the provider as a whole, not to any one of
its agents: `max_claim` is a budget across everything it hosts, exactly as it
is for one SDK process registering a batch.

Nothing here is network-shaped. `Worker` reaches souk through the same three
methods a remote transport carries for a remote provider — `claim_work`,
`report_event`, `finish_run` — and through nothing else.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from souk.errors import InvalidRegistration
from souk.identity import verify_session_token
from souk.models import AgentRef
from souk.providers import Provider

if TYPE_CHECKING:
    from souk.core import Souk

logger = logging.getLogger("souk.worker")


@dataclass(frozen=True)
class ClaimedRun:
    """A run a worker has taken, with everything needed to run it.

    Carries `run_input` because claiming *is* the hand-over — there is no
    second round trip to fetch the input. Under the pull model, claiming only
    discovered a run_id and core then called back into the provider to
    deliver the input; that callback is what made "has the input actually
    gone out yet" a question worth a handshake in the first place.

    Deliberately not a `broker.Run`: that is souk's own mutable dispatch
    state, and no worker — in-process or otherwise — should be holding it.
    """

    run_id: str
    agent: AgentRef
    thread_id: str
    run_input: dict[str, Any]


class Worker:
    """souk's own worker loop, driving one in-process provider.

    The same loop as the remote SDK's, against the same core methods — which
    is why it lives here rather than being left to each embedding caller. If
    in-process work took a shorter path than remote work, the shorter path
    would be the one that never gets the throttling, the identity check or
    the ordering right (see docs/library-architecture.md, "In-process is not
    trusted").

    One of these per attached provider, never per agent: `max_claim` is a
    budget across everything that provider hosts, the in-process counterpart
    of `PollRequest.max_claim`. `None` means unlimited — souk hands over its
    whole backlog for those agents on every claim, which is what an attached
    provider used to do with no way to say otherwise.
    """

    def __init__(
        self,
        souk: "Souk",
        session_token: str,
        renew_token: Callable[[], str],
        provider: Provider,
        agent_names: list[str],
        max_claim: int | None = None,
    ) -> None:
        self.souk = souk
        self.session_token = session_token
        # How this worker gets a fresh token when its current one expires:
        # souk.identity.SESSION_TOKEN_TTL_SECONDS is an hour and a worker
        # outlives that. The remote SDK does the same thing by re-registering
        # on reconnect; in-process, souk re-issues for the identity that
        # registered these agents.
        self.renew_token = renew_token
        self.provider = provider
        # Which of this provider's agents to claim for. Routing to the right
        # one is the provider's own job — it gets the name — so this is a list
        # of names, not a table of callables souk would look up. Names,
        # because within one provider a name is unique and the key is already
        # this worker's own.
        self.agent_names = list(agent_names)
        self.max_claim = max_claim
        self._in_flight: dict[str, asyncio.Task] = {}
        self._loop_task: asyncio.Task | None = None
        self.public_key = self._identify()

    def _identify(self) -> str:
        """Which provider this worker is — the public key its token was
        issued to. Every event it pushes is checked against the identity that
        claimed the run (see Souk.report_event), so this is load-bearing, not
        decoration, and it is read back out of the token rather than taken on
        trust from whoever constructed the worker."""
        public_key = verify_session_token(
            self.session_token, self.souk.settings.token_signing_secret
        )
        if public_key is None:
            raise InvalidRegistration("worker: missing or invalid session token")
        return public_key

    # ---- Lifecycle

    def start(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = self.souk.spawn(
                self.run_forever(), name=f"worker:{self.public_key}"
            )

    def stop(self) -> None:
        """Stop claiming new work. Runs already in flight are left alone:
        this worker is going away, but its agents are still producing, and
        souk must not record an outcome it hasn't observed."""
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None

    async def run_forever(self) -> None:
        """Claim, dispatch, repeat.

        The wait is always the claim call's own (it returns the moment work
        is enqueued for one of these agents — see RunBroker.subscribe_wake),
        never a sleep between claims: a worker that slept would leave new
        work sitting for up to an interval while doing nothing. What changes
        is how long it is willing to block. Idle, that is the full long poll.
        Busy, it is the short interval — because the thing it is waiting for
        then is its *own* capacity freeing up, which souk cannot observe and
        so cannot wake it for.

        Full is the one case that does sleep: there is nothing to ask for,
        and asking for zero runs is not a question souk can answer late.

        Claiming also refreshes `last_seen_at` for every agent this worker
        hosts (see Souk.claim_work), which is what keeps an in-process agent
        showing as online — the same signal, produced the same way, as a
        remote provider's polling. There is no separate in-process
        heartbeat any more; there is no separate in-process anything.
        """
        settings = self.souk.settings
        while True:
            try:
                capacity = self._capacity()
                full = capacity == 0
                claimed = await self.souk.claim_work(
                    self.session_token,
                    list(self.agent_names),
                    max_claim=capacity,
                    wait_seconds=(
                        0
                        if full
                        else settings.worker_poll_interval_seconds
                        if self._in_flight
                        else settings.worker_long_poll_seconds
                    ),
                    on_cancel=self.notify_cancel,
                )
                for run in claimed:
                    self._dispatch(run)
                if full:
                    await asyncio.sleep(settings.worker_poll_interval_seconds)
            except asyncio.CancelledError:
                raise
            except InvalidRegistration:
                # Almost always an expired token — get a new one and try
                # again rather than leaving these agents silently unserved.
                # If renewing itself fails, back off: a worker spinning on a
                # token it cannot fix would be a busy loop, and a silent one.
                try:
                    self.session_token = self.renew_token()
                    self.public_key = self._identify()
                except Exception:
                    logger.exception("worker %s: could not renew its token", self.public_key)
                    await asyncio.sleep(settings.worker_poll_interval_seconds)
            except Exception:
                logger.exception("worker %s: claim loop failed; retrying", self.public_key)
                await asyncio.sleep(self.souk.settings.worker_poll_interval_seconds)

    def _capacity(self) -> int | None:
        if self.max_claim is None:
            return None
        return max(0, self.max_claim - len(self._in_flight))

    # ---- Running one claimed run

    def _dispatch(self, run: ClaimedRun) -> None:
        task = self.souk.spawn(self._execute(run), name=f"run:{run.run_id}")
        self._in_flight[run.run_id] = task
        task.add_done_callback(lambda _task, run_id=run.run_id: self._in_flight.pop(run_id, None))

    async def _execute(self, run: ClaimedRun) -> None:
        """One run, start to finish, reported as it goes.

        `finish_run` is in a `finally` because it is the only thing that ends
        the run for souk: however this stops — the agent finishing, an
        exception, or this worker honouring a cancel — souk decides the
        outcome from what it actually saw (see handlers._handle_finish), and
        it cannot decide anything until it knows the stream is over.
        """
        if run.agent.name not in self.agent_names:
            # Only reachable if this provider's agent list changed between
            # the claim and now. Ending the run beats holding one nobody is
            # going to run.
            logger.warning(
                "worker %s: claimed run %s for '%s', which it no longer serves",
                self.public_key,
                run.run_id,
                run.agent,
            )
            self.souk.finish_run(run.run_id, claimed_by=self.public_key)
            return
        try:
            # The provider routes: it is given the name, because one provider
            # serving several agents is ordinary (see souk/providers.py) and
            # RunAgentInput carries no agent identity.
            async for event in self.provider.run_stream(run.agent.name, run.run_input):
                self.souk.report_event(run.run_id, event, claimed_by=self.public_key)
        except asyncio.CancelledError:
            logger.info("run %s: agent stopped", run.run_id)
            raise
        except Exception:
            logger.exception("run %s: agent failed", run.run_id)
        finally:
            # Synchronous on purpose: this also runs while unwinding a
            # cancellation, where an `await` here would be interrupted before
            # it ever reached souk and the run would hang until the stall
            # sweep noticed.
            self.souk.finish_run(run.run_id, claimed_by=self.public_key)

    def notify_cancel(self, run_id: str) -> None:
        """souk is asking for this run to stop. This worker complies, by
        cancelling the task running the agent — the only way to interrupt an
        arbitrary async generator, and exactly what the remote SDK does when
        it reads a cancel frame.

        Complying is *this worker's* choice, not souk's decision: core only
        publishes the request and then waits to see what the stream does. A
        worker that ignores it and finishes has finished, and souk records
        `completed` (see handlers._handle_finish); a different worker may
        legitimately implement this as a no-op.
        """
        task = self._in_flight.get(run_id)
        if task is not None:
            task.cancel()
