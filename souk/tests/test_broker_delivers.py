"""souk hands work over: the offer, the ack, and what souk does with a no.

The provider used to come and take work. It does not: the broker finds
whoever serves an agent, offers each run, and the provider's answer decides
what happens next. `True` is the ack and the run has started; anything else —
`False`, an exception, silence — leaves the run exactly where it was, queued,
to be offered again.

Everything here is in memory and no database is involved, which is what the
broker is: it holds runs, providers and one loop. Writing anything down is a
handler's job.

**Why this file exists at all.** After the inversion the suite still had 144
passing tests, and four separate breakages of this path changed that number by
zero:

    a decline treated as an ack (souk starts runs nobody took)  -> 144 passed
    the capacity place never returned (bucket leaks shut)       -> 144 passed
    every run delivered twice                                   -> 144 passed
    the delivery timeout removed entirely                       -> 144 passed

Not because those tests were vague — because the tests that drove a run all
went through `claim_work` and `attach_provider`'s old signature, so every one
of them was already failing for a reason that had nothing to do with whether
souk still delivers anything. The passing half was precisely the half that
never dispatches. Each of those four must turn something here red.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from souk.broker import Fail, RequestCancel, RunBroker
from souk.models import AgentRef

AGENT = AgentRef(provider_key="pk_provider", name="translator")
OTHER = AgentRef(provider_key="pk_provider", name="summarizer")


async def _until(predicate, timeout: float = 1.0) -> None:
    """Wait for the broker's loop to get somewhere, without assuming how many
    turns it takes. Raises TimeoutError, which is the failure — a delivery
    that never happens and a test that hangs are the same defect."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


class Recording:
    """A provider, as the broker sees one: a key, a way to take a run, a way
    to be asked to stop one. Deliberately not the SDK's runtime — this file
    is about what souk does with the answer, so the answer is scripted.

    `answers` is consumed one per offer, then `default` repeats. Recording
    every offer rather than a count, because "was this run offered twice" is
    one of the things that must be answerable.
    """

    def __init__(
        self,
        key: str = "pk_provider",
        *,
        max_concurrent_runs: int | None = None,
        answers: list[bool] | None = None,
        default: bool = True,
        hang: bool = False,
    ) -> None:
        self.public_key = key
        self.max_concurrent_runs = max_concurrent_runs
        self._answers = list(answers or [])
        self._default = default
        self._hang = hang
        self.offered: list[str] = []
        self.cancelled: list[str] = []

    async def deliver(self, run) -> bool:
        self.offered.append(run.run_id)
        if self._hang:
            await asyncio.Event().wait()  # never answers
        return self._answers.pop(0) if self._answers else self._default

    def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)


@pytest.fixture
async def broker():
    """A running broker. Started, because delivery only ever happens in its
    loop — nothing else in the class hands a run out, which is what keeps one
    run from being given to two providers."""
    b = RunBroker(deliver_timeout_seconds=0.05, queued_timeout_seconds=30)
    b.start()
    try:
        yield b
    finally:
        b.stop()


def _enqueue(broker: RunBroker, run_id: str, agent: AgentRef = AGENT):
    # `handlers={}` spawns the run's pipeline without giving it anything to
    # do: this file is about dispatch, and what a handler writes down is
    # tested where the handlers are.
    return broker.enqueue_run(run_id, agent, "thread_1", {"messages": []}, "ag-ui", {})


# ---- The ack


async def test_an_ack_starts_the_run_and_takes_a_place(broker):
    """True means started, and souk records it in the same step it hands the
    run over — there is no moment where a run has been given away and belongs
    to nobody."""
    provider = Recording()
    broker.register_provider({AGENT: provider})
    _enqueue(broker, "run_1")

    await _until(lambda: provider.offered == ["run_1"])
    await _until(lambda: broker.get("run_1").claimed_by == "pk_provider")

    assert broker.quality()["pk_provider"].in_flight == 1


async def test_the_provider_is_handed_a_value_not_soukes_dispatch_state(broker):
    """What crosses is a `ClaimedRun` — identity, agent, input — and never
    `broker.Run`, which has this run's queues hanging off it.

    Measured, not assumed: the broker did hand over its own `Run`, and the
    first time a real provider was given one it read `run.run_input` off an
    object whose field is `input_json` and the run died as RUN_ERROR. A
    provider holding that object could reach into souk's machinery in
    process, and could not be handed anything at all over a wire.
    """
    handed: list = []

    class Inspecting(Recording):
        async def deliver(self, run) -> bool:
            handed.append(run)
            return await super().deliver(run)

    broker.register_provider({AGENT: Inspecting()})
    _enqueue(broker, "run_1")
    await _until(lambda: bool(handed))

    run = handed[0]
    assert (run.run_id, run.agent, run.run_input) == ("run_1", AGENT, {"messages": []})
    assert not hasattr(run, "in_queue") and not hasattr(run, "out_queue")


async def test_a_run_is_delivered_exactly_once(broker):
    """Two deliveries of one run is two agents producing for it, and souk
    would record whichever finished first.

    The shape that caused it: `_offer_pending` reads the head of the queue,
    awaits the provider, and removes it only after — so two callers both see
    the same run at the head and both hand it over, and the second `popleft`
    removes the *next* run along, which is thereby lost. Not delayed. Lost:
    `expire_queued` only looks at the pending queue, so a run taken out of it
    and given to nobody is never offered again and never given up on either.
    """
    provider = Recording()
    broker.register_provider({AGENT: provider})
    for i in range(5):
        _enqueue(broker, f"run_{i}")

    await _until(lambda: len(provider.offered) >= 5)
    await asyncio.sleep(0.05)  # let the loop turn again in case it re-offers

    assert sorted(provider.offered) == [f"run_{i}" for i in range(5)]


# ---- The no


async def test_a_decline_leaves_the_run_queued(broker):
    """A run that was not taken has not started. The provider is the only one
    that knows whether it can take work, so `False` is believed."""
    provider = Recording(answers=[False, False], default=False)
    broker.register_provider({AGENT: provider})
    _enqueue(broker, "run_1")

    await _until(lambda: bool(provider.offered))
    await asyncio.sleep(0.05)

    assert broker.get("run_1").claimed_by is None
    assert broker.quality()["pk_provider"].in_flight == 0


async def test_a_declined_run_is_offered_again_once_something_changes(broker):
    """Declining is "not now", not "never". What must not happen is asking
    again immediately — that is the same question to a provider whose answer
    cannot have changed — so the loop waits for something that could change
    it. A run arriving is one of those things."""
    provider = Recording(answers=[False], default=True)
    broker.register_provider({AGENT: provider})
    _enqueue(broker, "run_1")
    await _until(lambda: provider.offered == ["run_1"])

    _enqueue(broker, "run_2")  # sets _work_to_do

    await _until(lambda: broker.get("run_1").claimed_by == "pk_provider")


async def test_declining_while_souk_believed_there_was_room_is_recorded(broker):
    """Two facts souk saw itself: the provider said it could take two, and it
    refused the first. Believe the provider — it is the one that knows — and
    record that souk had to find out by being refused."""
    provider = Recording(max_concurrent_runs=2, default=False)
    broker.register_provider({AGENT: provider})
    _enqueue(broker, "run_1")

    await _until(lambda: bool(provider.offered))
    await _until(lambda: broker.quality()["pk_provider"].misdeclared == 1)

    quality = broker.quality()["pk_provider"]
    assert quality.declared == 2
    # Treated as full from here: souk stops offering rather than asking a
    # provider that just refused, once per queued run.
    assert quality.in_flight == 2


async def test_a_provider_that_never_answers_is_not_waited_on_forever(broker):
    """There is one delivery loop, so an offer that never returns stops
    dispatch for every agent — not only this provider's. The timeout is what
    keeps one silent provider from being an outage.

    The run stays queued rather than being retried quietly, and the silence
    is counted: if the provider did take it, offering it again runs it twice,
    so souk records that it does not know instead of guessing.
    """
    silent = Recording(key="pk_silent", hang=True)
    broker.register_provider({AGENT: silent})
    _enqueue(broker, "run_1")

    await _until(lambda: broker.quality()["pk_silent"].unanswered >= 1, timeout=2.0)
    assert broker.get("run_1").claimed_by is None

    # And the loop is still alive for everyone else.
    working = Recording(key="pk_working")
    broker.register_provider({OTHER: working})
    _enqueue(broker, "run_2", OTHER)
    await _until(lambda: working.offered == ["run_2"], timeout=2.0)


# ---- The bucket


async def test_a_full_provider_is_offered_nothing(broker):
    """Capacity is the provider's own number, held by souk so it can stop
    asking. One bucket per identity however many agents it serves, because
    one process is one budget."""
    provider = Recording(max_concurrent_runs=1)
    broker.register_provider({AGENT: provider, OTHER: provider})
    _enqueue(broker, "run_1", AGENT)
    _enqueue(broker, "run_2", OTHER)

    await _until(lambda: len(provider.offered) == 1)
    await asyncio.sleep(0.05)

    assert len(provider.offered) == 1, "offered past a full provider's declared capacity"


async def test_the_place_comes_back_when_the_run_ends(broker):
    """Every ending passes through `forget` — finished, failed, cancelled,
    reaped — which is why the place is returned there rather than somewhere
    more specific. A place returned on only some endings is a bucket that
    empties permanently on the rest, and a provider that silently stops being
    offered anything."""
    provider = Recording(max_concurrent_runs=1)
    broker.register_provider({AGENT: provider})
    _enqueue(broker, "run_1")
    _enqueue(broker, "run_2")
    await _until(lambda: provider.offered == ["run_1"])

    broker.forget("run_1")

    await _until(lambda: provider.offered == ["run_1", "run_2"])
    assert broker.quality()["pk_provider"].in_flight == 1


async def test_a_reconnecting_provider_keeps_its_bucket(broker):
    """Registering again is what a reconnect is, and the runs it already holds
    are still its own. Resetting the count would let souk offer past its
    capacity every time a connection blipped."""
    provider = Recording(max_concurrent_runs=1)
    broker.register_provider({AGENT: provider})
    _enqueue(broker, "run_1")
    await _until(lambda: broker.quality()["pk_provider"].in_flight == 1)

    broker.register_provider({AGENT: provider})

    assert broker.quality()["pk_provider"].in_flight == 1


# ---- Provider quality: things souk saw, not things it inferred


async def test_a_late_ack_is_accepted_from_the_provider_that_owns_the_agent(broker):
    """The events are the proof — nothing else could produce them — so this is
    the ack arriving after souk stopped waiting.

    Counted as the provider's doing rather than the network's: the transport
    is ordered, so an answer that arrives after the offer timed out is a
    provider that was slow, not a frame that overtook another.
    """
    silent = Recording(hang=True)
    broker.register_provider({AGENT: silent})
    _enqueue(broker, "run_1")
    await _until(lambda: broker.quality()["pk_provider"].unanswered >= 1, timeout=2.0)

    assert broker.accept_late_ack("run_1", "pk_provider") is True

    quality = broker.quality()["pk_provider"]
    assert (quality.answered_late, quality.in_flight) == (1, 1)
    assert broker.get("run_1").claimed_by == "pk_provider"


async def test_a_late_ack_from_anyone_else_is_refused(broker):
    """Otherwise it is a way to take over any run by guessing a run_id."""
    silent = Recording(hang=True)
    broker.register_provider({AGENT: silent})
    _enqueue(broker, "run_1")
    await _until(lambda: broker.quality()["pk_provider"].unanswered >= 1, timeout=2.0)

    assert broker.accept_late_ack("run_1", "pk_impostor") is False
    assert broker.get("run_1").claimed_by is None


async def test_taking_a_run_and_never_ending_it_is_recorded(broker):
    """souk saw both halves — it handed the run over, and the health sweep had
    to give up on it — so it can say so. The place was held the whole time."""
    provider = Recording()
    broker.register_provider({AGENT: provider})
    _enqueue(broker, "run_1")
    await _until(lambda: broker.get("run_1").claimed_by == "pk_provider")

    broker.push("run_1", Fail("stalled"))

    assert broker.quality()["pk_provider"].abandoned == 1


async def test_a_run_nobody_ever_takes_is_given_up_on(broker):
    """A run with no provider registered at all has nowhere to go, and the
    caller is still watching. `queued_at` is held in memory because it never
    changes, so the broker answers its own question — how long have I held
    this — without reading anything."""
    _enqueue(broker, "run_1")

    expired = broker.expire_queued(timeout_seconds=0)

    assert expired == ["run_1"]
    await _until(lambda: broker.get("run_1") is None)


# ---- Cancelling, which the broker decides between because only it knows


async def test_cancelling_a_delivered_run_keeps_the_provider_in_the_loop(broker):
    """souk can ask; it cannot compel. The request goes onto the run's own
    queue in order behind everything else about it, and the way to reach the
    provider was recorded when it took the run — a run's cancel goes to
    whoever actually has it, not to whoever serves that agent now.
    """
    provider = Recording()
    broker.register_provider({AGENT: provider})
    _enqueue(broker, "run_1")
    await _until(lambda: broker.get("run_1").claimed_by == "pk_provider")

    assert broker.request_cancel("run_1") is True

    snapshot = broker.get("run_1")
    assert snapshot.cancel_requested is True
    # Still dispatching it: the outcome is decided when the stream ends, not
    # when the request is made. Recording `cancelled` here would be a lie the
    # run's own output could contradict.
    assert snapshot.claimed_by == "pk_provider"


async def test_cancelling_a_queued_run_stops_it_ever_being_offered(broker):
    """Nobody has it, so there is nobody to ask — and nothing should be
    started on behalf of a caller who has already said stop.

    `cancel_requested` is set synchronously, before either path, so the loop
    cannot hand it out in the meantime.
    """
    provider = Recording()
    _enqueue(broker, "run_1")

    assert broker.request_cancel("run_1") is True
    broker.register_provider({AGENT: provider})
    await asyncio.sleep(0.05)

    assert provider.offered == []


async def test_a_cancelled_run_does_not_block_the_one_behind_it(broker):
    """The queue is oldest-first, so a run nobody will ever take sits at the
    head of it. It has to be stepped over rather than stopping the line."""
    provider = Recording()
    _enqueue(broker, "run_1")
    _enqueue(broker, "run_2")
    broker.request_cancel("run_1")
    broker.register_provider({AGENT: provider})

    await _until(lambda: provider.offered == ["run_2"])


async def test_the_place_comes_back_when_a_delivered_run_is_cancelled(broker):
    """The ending that is easiest to leak: `forget` is reached from the
    pipeline for a finish, and from a one-shot for a cancelled queued run.
    A bucket that only empties on success is one that fills up permanently on
    a provider whose runs get cancelled."""
    provider = Recording(max_concurrent_runs=1)
    broker.register_provider({AGENT: provider})
    _enqueue(broker, "run_1")
    _enqueue(broker, "run_2")
    await _until(lambda: provider.offered == ["run_1"])

    broker.request_cancel("run_1")
    broker.push("run_1", Fail("cancelled"))
    broker.forget("run_1")

    await _until(lambda: provider.offered == ["run_1", "run_2"])


def test_a_broker_can_be_built_outside_a_loop_and_started_in_two(caplog):
    """One RunBroker, constructed with no event loop running, then started
    and stopped in two different ones.

    `RunBroker` is built synchronously — `Souk.__init__` makes one, and that
    can happen at import time or in a fixture. Its wake event binds to the
    loop that first waits on it, so keeping the one made in `__init__` made
    every later loop raise "bound to a different event loop" the moment the
    sweep tried to rest.

    Asserted on the log rather than on a result, because the failure is
    invisible from outside: `run_forever` catches everything so one bad
    provider cannot stop dispatch for everyone, so this got caught, logged,
    and retried a second later. Runs still went out. What broke was resting —
    the broker degraded to a one-second poll that threw an exception every
    round, and every test still passed.

    Not covered by anything else here: every other test builds its broker
    inside its own loop, which is the case that works.
    """
    broker = RunBroker(deliver_timeout_seconds=0.05)

    async def place_one(run_id: str) -> list[str]:
        provider = Recording()
        broker.start()
        try:
            broker.register_provider({AGENT: provider})
            _enqueue(broker, run_id)
            await _until(lambda: provider.offered == [run_id])
            # Let it go round again with nothing to place, which is when it
            # rests on the event — the only moment the binding matters.
            await asyncio.sleep(0.05)
            return provider.offered
        finally:
            broker.forget(run_id)
            broker.stop()

    with caplog.at_level(logging.ERROR, logger="souk.broker"):
        assert asyncio.run(place_one("run_1")) == ["run_1"]
        assert asyncio.run(place_one("run_2")) == ["run_2"]

    assert [r.getMessage() for r in caplog.records] == []
