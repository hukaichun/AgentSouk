"""What actually happens when a Command is applied to a Run.

These are the "function objects" broker.py's pipeline model dispatches to —
see its module docstring for why exactly one task per run applies them, and
why nothing else may touch a Run's fields. `make_handlers` builds the
dispatch table for one Souk.

This module used to live in the serving layer, and was the one genuine
transport leak in souk: three of these handlers built wire frames directly.
Everything they do is domain logic — persist, reduce, decide a status — so
they belong in core. Nothing here imports a transport, and nothing here calls
out to a worker either: events arrive as commands, pushed by whoever holds the
run (see souk/worker.py).
"""

from __future__ import annotations

import contextlib
import logging
from functools import partial
from typing import TYPE_CHECKING

from souk import repo
from souk.agui_reduce import reduce_events_to_messages
from souk.broker import (
    END_OF_STREAM,
    Claim,
    Fail,
    FinishStream,
    HandlerMap,
    RelayEvent,
    RequestCancel,
    Run,
)
from souk.pause import interrupt_outcome_of

if TYPE_CHECKING:
    from souk.core import Souk

logger = logging.getLogger("souk.handlers")


async def _handle_claim(souk: "Souk", run: Run, cmd: Claim) -> None:
    """A worker has taken this run. All that is left to do is record it.

    The hand-over itself already happened, synchronously, in
    broker.RunBroker.claim — the worker left with the run's input in hand.
    So this is not the moment the run starts; it is the moment souk writes
    down that it did, in order against everything else on this run's
    pipeline (a cancel queued behind it will see a claimed run, which is
    what makes _handle_cancel's two cases unambiguous).

    This used to call `provider.start(...)` and spawn a pump task to drain
    the returned stream back into the queue this handler was itself
    dispatched from. Both are gone: the worker pushes.
    """
    async with souk.session() as session:
        await souk.mark_run_status(session, run.run_id, "running")


async def _handle_relay(souk: "Souk", run: Run, cmd: RelayEvent) -> None:
    # Persist *before* relaying: if souk crashes between the two, the
    # live caller must not end up having seen an event that was never
    # durably recorded.
    event = cmd.event
    if event.get("type") == "RUN_FINISHED":
        # The agent finished on its own terms. Remembered rather than acted
        # on: RUN_FINISHED is not itself a stream terminator (see
        # souk/pause.py), and _handle_finish is where the run's outcome is
        # decided once the stream really ends.
        run.saw_run_finished = True
        interrupts = interrupt_outcome_of(event)
        if interrupts is not None:
            run.pause_payload = {"interrupts": interrupts}
    elif event.get("type") == "RUN_ERROR":
        # The agent reported its own failure, so the caller has been told.
        # See _handle_finish, which speaks up only when nobody did.
        run.saw_run_error = True
    run.seq += 1
    async with souk.session() as session:
        await repo.append_run_event(session, run.run_id, run.seq, event)
        # Marks the run as still making progress — see
        # repo.fail_stalled_runs, which would otherwise eventually treat
        # a quiet-but-alive run as abandoned.
        await repo.touch_run_activity(session, run.run_id)
        await session.commit()
    await run.out_queue.put(event)


async def _handle_finish(souk: "Souk", run: Run, cmd: FinishStream) -> None:
    """The worker says this run's stream has ended. This is where — and only
    where — a run's outcome is decided, because until now souk could not
    honestly know it: asking a worker to stop is a request, and it is free to
    ignore the request and finish normally.

    AG-UI gives no cancellation signal to read: its terminal events are
    RUN_FINISHED (outcome success or interrupt) and RUN_ERROR, with no
    cancelled event or outcome. So the only evidence that a run did *not*
    finish on its own terms is the absence of RUN_FINISHED, and what
    distinguishes "stopped because we asked" from "stopped because it broke"
    is whether souk asked:

        RUN_FINISHED, interrupt outcome  -> input-required
        RUN_FINISHED                     -> completed  (even if a cancel was
                                            requested — it finished anyway)
        no RUN_FINISHED, cancel asked    -> cancelled
        no RUN_FINISHED, nothing asked   -> failed

    A `failed` verdict is also *told to the caller*, as a terminal RUN_ERROR,
    when nothing else has. Recording it and staying quiet was the observable
    bug: a provider whose run_stream raised produced an HTTP 200 with an
    empty event stream that closed in 0.1s — indistinguishable, to a client,
    from an agent with nothing to say. This is not souk deciding anything on
    a provider's behalf; the verdict above is already souk's own and already
    persisted. It is souk saying out loud what it just wrote down.

    `cancelled` deliberately gets no such event: AG-UI has no cancelled
    event or outcome to send (see this docstring's second paragraph), and the
    party that would read it is the same party that asked.
    """
    if run.pause_payload is not None:
        status, metadata = "input-required", run.pause_payload
    elif run.saw_run_finished:
        status, metadata = "completed", None
    elif run.cancel_requested:
        status, metadata = "cancelled", None
    else:
        status, metadata = "failed", {"failureReason": "provider_stream_ended_without_finishing"}

    # RunErrorEvent's own schema is just type/message/code — no thread_id or
    # run_id fields to fill in, same as the agent-offline event in
    # protocols/agui.
    failure_event = (
        {
            "type": "RUN_ERROR",
            "message": "the agent's stream ended without finishing",
            "code": "provider_stream_ended_without_finishing",
        }
        if status == "failed" and not run.saw_run_error
        else None
    )

    async with souk.session() as session:
        await souk.mark_run_status(session, run.run_id, status, metadata=metadata)
        if status in ("completed", "input-required"):
            # A genuine reply was produced (as opposed to failed/cancelled —
            # see souk/agui_reduce.py's module docstring for why those don't
            # go through this at all) — persist it as real thread_history
            # messages so souk is an actual source of truth for the full
            # conversation, not just the caller's half of it. Only this
            # round's own events (see Run.round_starting_seq) — a resumed
            # run's earlier round(s) were already persisted the first time
            # they finished.
            round_events = await repo.get_run_events(session, run.run_id, since_seq=run.round_starting_seq)
            reply_messages = reduce_events_to_messages(round_events)
            if reply_messages:
                await repo.append_thread_messages(session, run.thread_id, run.run_id, reply_messages)
        if failure_event is not None:
            # Persisted as a real run event, not only relayed: a caller that
            # reconnects and reads the run's stored events must get the same
            # account as one that stayed on the stream. It also reaches A2A
            # for free — RUN_ERROR is already translated to a final `failed`
            # status update (see protocols/a2a_translate).
            run.seq += 1
            await repo.append_run_event(session, run.run_id, run.seq, failure_event)
        await session.commit()
    # Nothing goes back to the agent here. souk used to send an `ack=true`
    # acknowledgement at this point, once everything was persisted; it was
    # removed because the agent could only ever log it — it has already
    # produced and discarded its events, so there is no recovery action
    # available to it if souk failed to persist. Whether a run is durable is a
    # question its *caller* asks, via the run's own status.
    if failure_event is not None:
        await run.out_queue.put(failure_event)
    await run.out_queue.put(END_OF_STREAM)


async def _handle_cancel(souk: "Souk", run: Run, cmd: RequestCancel) -> None:
    """Cancelling is only ever one of two situations, and which one it is
    is unambiguous by the time this runs — Claim and RequestCancel are
    processed in order on the run's own pipeline task, so "has a worker
    got this yet" is already settled:

    1. **Nobody has it.** No worker is running it, so nothing has to be
       asked of anyone and souk can record `cancelled` outright — it is the
       only party involved, so it knows. broker._pipeline treats this as
       terminal and forgets the run right after this returns.

    2. **A worker has it.** souk *asks* it to stop and records `cancelling`,
       not `cancelled`: the worker may honour the request, may take a while,
       or may ignore it and finish normally, and souk cannot know which
       until the stream ends. The outcome is decided there instead (see
       _handle_finish). Nothing is torn down here — whatever the agent
       emits between now and then is real output and is persisted and
       relayed like any other.

    Asking is a plain synchronous notification (see Run.cancel_notify),
    which is all a request can honestly be. It used to be
    `await provider.cancel(run_id)` — an await, on this run's pipeline
    task, into provider code, which meant a slow or wedged provider could
    stall the very queue its own events arrive on.

    `run.cancel_requested` was already set synchronously by
    broker.request_cancel before this was queued (do not set it here too —
    broker.claim depends on observing it true before this handler has had a
    chance to run at all; see that field's docstring).
    """
    if run.claimed_by is None:
        async with souk.session() as session:
            await souk.mark_run_status(session, run.run_id, "cancelled")
        await run.out_queue.put(END_OF_STREAM)
        return

    async with souk.session() as session:
        await souk.mark_run_status(session, run.run_id, "cancelling")
    if run.cancel_notify is not None:
        # A worker's own code: it must never be able to break this run's
        # pipeline, and there is nothing souk could do about it anyway.
        with contextlib.suppress(Exception):
            run.cancel_notify(run.run_id)


async def _handle_fail(souk: "Souk", run: Run, cmd: Fail) -> None:
    """The health sweep gave up on this run — see souk/health.py."""
    event = {"type": "RUN_ERROR", "message": cmd.reason}
    run.seq += 1
    async with souk.session() as session:
        await repo.append_run_event(session, run.run_id, run.seq, event)
        await souk.mark_run_status(session, run.run_id, "failed", metadata={"failureReason": cmd.reason})
    await run.out_queue.put(event)
    await run.out_queue.put(END_OF_STREAM)


def make_handlers(souk: "Souk") -> HandlerMap:
    """The dispatch table every run's pipeline task uses, bound to one
    souk instance. A factory rather than a module-level dict because the
    handlers need a database to write to, and that comes from an
    explicitly-constructed Souk rather than an import-time global (see
    souk/core.py).
    """
    return {
        Claim: partial(_handle_claim, souk),
        RelayEvent: partial(_handle_relay, souk),
        FinishStream: partial(_handle_finish, souk),
        RequestCancel: partial(_handle_cancel, souk),
        Fail: partial(_handle_fail, souk),
    }
