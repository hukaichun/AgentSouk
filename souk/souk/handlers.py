"""What actually happens when a Command is applied to a Run.

These are the "function objects" broker.py's pipeline model dispatches to —
see its module docstring for why exactly one task per run applies them, and
why nothing else may touch a Run's fields. `make_handlers` builds the
dispatch table for one Souk.

This module used to live in grpc_server.py, and was the one genuine transport
leak in souk: three of these handlers constructed `souk_pb2.AgentEventEnvelope`
protobuf messages directly. Everything they do is domain logic — persist,
reduce, decide a status — so they belong in core, and talking to the agent
goes through the AgentProvider port instead (see souk/providers.py). Nothing
here imports a transport.
"""

from __future__ import annotations

import asyncio
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
from souk.providers import AgentProvider

if TYPE_CHECKING:
    from souk.core import Souk

logger = logging.getLogger("souk.handlers")


async def _pump(souk: "Souk", run: Run, stream) -> None:
    """Drains the agent's event stream into this run's in_queue.

    The bridge between the provider port (pull: iterate a stream) and the
    broker's pipeline (push: commands on a queue). Runs as its own task so
    the pipeline stays free to process cancels while the agent is producing.

    Reads until the provider's stream ends, and only then pushes
    FinishStream. souk never tears this down to force a run to end — not
    even for a cancel. Asking a provider to stop is a request (see
    _handle_cancel); whether and when it actually stops is the provider's
    business, and anything it emits in the meantime is real output that gets
    persisted and relayed like any other. A provider that ignores the
    request and runs to completion has, honestly, completed.
    """
    try:
        async for event in stream:
            run.in_queue.put_nowait(RelayEvent(event))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("run %s: provider stream failed", run.run_id)
    finally:
        run.in_queue.put_nowait(FinishStream())


async def _handle_claim(souk: "Souk", run: Run, cmd: Claim) -> None:
    if run.cancel_requested:
        # The request arrived before this run ever reached a provider, so
        # there is nobody to ask to stop — don't hand it over at all.
        # (poll() already refuses to hand out such a run; this covers the
        # narrow window where the request landed after poll() gave it out
        # but before this claim was processed.) The queued RequestCancel
        # behind this Claim records the outcome.
        run.in_queue.put_nowait(FinishStream())
        return
    run.provider = cmd.provider
    async with souk.session() as session:
        await repo.mark_run_status(session, run.run_id, "running")
    # `start` returning means the agent really has its input — for a remote
    # one, that the frame is on the wire.
    stream = await cmd.provider.start(run.agent_id, run.input_json)
    run.pump_task = souk.spawn(_pump(souk, run, stream), name=f"pump:{run.run_id}")


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
    """The provider's stream has ended. This is where — and only where — a
    run's outcome is decided, because until now souk could not honestly know
    it: asking a provider to stop is a request, and a provider is free to
    ignore it and finish normally.

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
    """
    if run.pause_payload is not None:
        status, metadata = "input-required", run.pause_payload
    elif run.saw_run_finished:
        status, metadata = "completed", None
    elif run.cancel_requested:
        status, metadata = "cancelled", None
    else:
        status, metadata = "failed", {"failureReason": "provider_stream_ended_without_finishing"}

    async with souk.session() as session:
        await repo.mark_run_status(session, run.run_id, status, metadata=metadata)
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
        await session.commit()
    # Nothing goes back to the agent here. souk used to send an `ack=true`
    # envelope at this point, once everything was persisted; it was removed
    # because the agent could only ever log it — it has already produced and
    # discarded its events, so there is no recovery action available to it if
    # souk failed to persist. Whether a run is durable is a question its
    # *caller* asks, via the run's own status. See proto/souk.proto's
    # reserved field 5.
    await run.out_queue.put(END_OF_STREAM)


async def _handle_cancel(souk: "Souk", run: Run, cmd: RequestCancel) -> None:
    """Cancelling is only ever one of two situations, and which one it is
    is unambiguous by the time this runs — Claim and RequestCancel are
    processed in order on the run's own pipeline task, so "has a provider
    got this yet" is already settled:

    1. **No provider has it.** Nobody is working on the run, so nothing has
       to be asked of anyone and souk can record `cancelled` outright — it
       is the only party involved, so it knows. broker._pipeline treats this
       as terminal and forgets the run right after this returns.

    2. **A provider has it.** souk *asks* it to stop and records
       `cancelling`, not `cancelled`: the provider may honour the request,
       may take a while, or may ignore it and finish normally, and souk
       cannot know which until its stream ends. The outcome is decided
       there instead (see _handle_finish). Nothing is torn down here —
       whatever the agent emits between now and then is real output and is
       persisted and relayed like any other.

    `run.cancel_requested` was already set synchronously by
    broker.request_cancel before this was queued (do not set it here too —
    _handle_claim depends on observing it true before this handler has had a
    chance to run at all; see that field's docstring).
    """
    if run.provider is None:
        async with souk.session() as session:
            await repo.mark_run_status(session, run.run_id, "cancelled")
        await run.out_queue.put(END_OF_STREAM)
        return

    async with souk.session() as session:
        await repo.mark_run_status(session, run.run_id, "cancelling")
    with contextlib.suppress(Exception):
        await run.provider.cancel(run.run_id)


async def _handle_fail(souk: "Souk", run: Run, cmd: Fail) -> None:
    """The health sweep gave up on this run — see souk/health.py."""
    event = {"type": "RUN_ERROR", "message": cmd.reason}
    run.seq += 1
    async with souk.session() as session:
        await repo.append_run_event(session, run.run_id, run.seq, event)
        await repo.mark_run_status(session, run.run_id, "failed", metadata={"failureReason": cmd.reason})
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
