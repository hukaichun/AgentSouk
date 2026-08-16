"""The provider's own loop: claim work, run it, push events back.

    while True:
        runs = await souk.claim_work(token, names, max_claim=capacity)
        for run in runs:
            spawn(execute(run))

**The loop belongs to the provider, and that is not duplication to remove.**
souk ships one of these too (`souk/worker.py`) for a provider sharing its
process, and this is a second implementation of one *contract*, not one piece
of knowledge written twice. Everything the loop decides is a provider-side
policy: how much capacity to declare, how often to ask, whether to comply
with a cancel, whether to keep waiting. souk's own architecture notes say so
— "Whether a worker complies is the worker's business... Both are
provider-side decisions." A design where the server drives this on the
worker's behalf has taken the loop away from its owner, and turns "it claimed"
— a domain act — into "its socket is open", a transport fact.

`souk` here is anything with the three methods below. A `Souk` object
satisfies it structurally, so an in-process provider passes one directly; a
remote one passes a client that carries the same three calls over a wire.
Nothing in this module imports souk, and it needs no database.

Two things differ from souk's in-process copy, and both are corrections
rather than variations:

- **Pacing is this worker's.** souk's copy reads its intervals out of
  `CoreSettings`, which is souk deciding how often a provider should ask.
- **Identity comes from the keypair.** souk's copy reads its own public key
  back out of its session token using souk's *signing secret* — which a
  remote provider does not have and must never have. A provider knows who it
  is because it holds the private key.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from souk_provider_sdk.identity import ProviderIdentity
from souk_provider_sdk.provider import Provider

logger = logging.getLogger("souk_provider_sdk.worker")


class SoukConnection(Protocol):
    """The three calls, and nothing else — souk's whole contract with a
    worker. Whatever carries them is not this module's business."""

    async def claim_work(
        self,
        session_token: str,
        agent_names: list[str],
        *,
        max_claim: int | None = ...,
        wait_seconds: float = ...,
        on_cancel: Callable[[str], None] | None = ...,
    ) -> list[Any]: ...

    def report_event(self, run_id: str, event: Any, *, claimed_by: str) -> bool: ...

    def finish_run(self, run_id: str, *, claimed_by: str) -> bool: ...


class ProviderWorker:
    """One of these per provider, never per agent: `max_claim` is a budget
    across everything it hosts."""

    def __init__(
        self,
        souk: SoukConnection,
        identity: ProviderIdentity,
        provider: Provider,
        agent_names: list[str],
        session_token: str,
        renew_token: Callable[[], Awaitable[str]] | None = None,
        max_claim: int | None = None,
        poll_interval_seconds: float = 2.0,
        long_poll_seconds: float = 25.0,
    ) -> None:
        self.souk = souk
        self.identity = identity
        self.provider = provider
        self.agent_names = list(agent_names)
        self.session_token = session_token
        # How this worker gets a fresh token when its own expires. Optional:
        # a worker that cannot renew simply stops, loudly, instead of
        # spinning on a token it cannot fix.
        self.renew_token = renew_token
        self.max_claim = max_claim
        self.poll_interval_seconds = poll_interval_seconds
        self.long_poll_seconds = long_poll_seconds
        self._in_flight: dict[str, asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()
        self._loop_task: asyncio.Task | None = None
        # Whether souk currently knows none of these names. State rather than
        # a counter, so the condition is reported when it starts and when it
        # ends rather than once per cycle.
        self._unowned = False

    @property
    def public_key(self) -> str:
        """Who this worker speaks as. From the keypair, not from a token."""
        return self.identity.public_key

    # ---- Lifecycle

    def start(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = self._spawn(self.run_forever(), name=f"worker:{self.public_key[:16]}")

    def stop(self) -> None:
        """Stop claiming. Runs already in flight are left alone: this worker
        is going away, but its agents are still producing, and souk must not
        record an outcome nobody observed."""
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None

    def _spawn(self, coro, *, name: str | None = None) -> asyncio.Task:
        # The loop keeps only a weak reference to a running task, so one
        # nothing else holds can be collected mid-flight.
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def run_forever(self) -> None:
        """Claim, dispatch, repeat.

        The wait is the claim call's own — it returns the moment work is
        enqueued — never a sleep between claims: a worker that slept would
        leave new work sitting for up to an interval while doing nothing.
        What changes is how long it is willing to block. Idle, the full long
        poll. Busy, the short interval, because what it is waiting for then is
        its *own* capacity, which souk cannot observe and so cannot wake it
        for. Full is the one case that sleeps: asking for zero runs is not a
        question souk can answer late.
        """
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
                        else self.poll_interval_seconds
                        if self._in_flight
                        else self.long_poll_seconds
                    ),
                    on_cancel=self.notify_cancel,
                )
                if self._unowned:
                    logger.info("souk knows these agents again — resuming normal claiming")
                    self._unowned = False
                for run in claimed:
                    self._dispatch(run)
                if full:
                    await asyncio.sleep(self.poll_interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._handle_claim_failure(exc)

    async def _handle_claim_failure(self, exc: Exception) -> None:
        """souk's refusals reach a remote worker as whatever its transport
        makes of them, so this matches on the *name* rather than importing
        souk's exception classes — the same reason the signing payloads are
        this package's own."""
        name = type(exc).__name__
        if name == "NothingOwned":
            # Nothing to claim, ever, until someone registers these names
            # again. Stay alive and be loud: the names do not change, so this
            # worker picks straight back up with nothing to reconfigure.
            if not self._unowned:
                logger.error(
                    "souk has registered none of this provider's agent names %s — "
                    "claiming nothing, and these agents are not on the roster. "
                    "Register them again; this worker resumes on its own.",
                    self.agent_names,
                )
                self._unowned = True
            await asyncio.sleep(self.long_poll_seconds)
        elif name == "InvalidRegistration" and self.renew_token is not None:
            try:
                self.session_token = await self.renew_token()
            except Exception:
                logger.exception("could not renew the session token")
                await asyncio.sleep(self.poll_interval_seconds)
        else:
            logger.exception("claim loop failed; retrying", exc_info=exc)
            await asyncio.sleep(self.poll_interval_seconds)

    def _capacity(self) -> int | None:
        if self.max_claim is None:
            return None
        return max(0, self.max_claim - len(self._in_flight))

    # ---- Running one claimed run

    def _dispatch(self, run: Any) -> None:
        task = self._spawn(self._execute(run), name=f"run:{run.run_id}")
        self._in_flight[run.run_id] = task
        task.add_done_callback(lambda _t, run_id=run.run_id: self._in_flight.pop(run_id, None))

    async def _execute(self, run: Any) -> None:
        """One run, start to finish, reported as it goes.

        `finish_run` is in a `finally` because it is the only thing that ends
        the run for souk: however this stops — the agent finishing, an
        exception, or this worker honouring a cancel — souk decides the
        outcome from what it saw, and cannot decide anything until it knows
        the stream is over.
        """
        name = run.agent.name if hasattr(run.agent, "name") else run.agent
        if name not in self.agent_names:
            logger.warning("claimed a run for '%s', which this provider no longer serves", name)
            self.souk.finish_run(run.run_id, claimed_by=self.public_key)
            return
        try:
            async for event in self.provider.run_stream(name, run.run_input):
                self.souk.report_event(run.run_id, event, claimed_by=self.public_key)
        except asyncio.CancelledError:
            logger.info("run %s: agent stopped", run.run_id)
            raise
        except Exception:
            logger.exception("run %s: agent failed", run.run_id)
        finally:
            # Synchronous on purpose: this also runs while unwinding a
            # cancellation, where an `await` would be interrupted before it
            # ever reached souk and the run would hang until the stall sweep
            # noticed.
            self.souk.finish_run(run.run_id, claimed_by=self.public_key)

    def notify_cancel(self, run_id: str) -> None:
        """souk is asking for this run to stop. Complying is *this worker's*
        choice, not souk's decision: core publishes the request and then waits
        to see what the stream does. A worker that ignores it and finishes has
        finished."""
        task = self._in_flight.get(run_id)
        if task is not None:
            task.cancel()
