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
from souk.providers import AgentProvider, open_run

if TYPE_CHECKING:
    from souk.core import Souk

logger = logging.getLogger("souk.handlers")


async def _pump(souk: "Souk", run: Run, stream) -> None:
    """Drains the agent's event stream into this run's in_queue.

    The bridge between the provider port (pull: iterate a stream) and the
    broker's pipeline (push: commands on a queue). Runs as its own task so
    the pipeline stays free to process cancels while the agent is producing.

    Always ends by pushing FinishStream, whether the agent finished on its
    own or this task was cancelled — the pipeline needs that either way to
    terminate the run, and _handle_finish already distinguishes the two by
    checking `run.cancelled`. Closing the stream on the way out is what
    tells the provider to stop, which for a remote agent is the cancel
    signal going back over the wire.
    """
    try:
        async for event in stream:
            run.in_queue.put_nowait(RelayEvent(event))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("run %s: provider stream failed", run.run_id)
    finally:
        with contextlib.suppress(Exception):
            await stream.aclose()
        run.in_queue.put_nowait(FinishStream())


async def _handle_claim(souk: "Souk", run: Run, cmd: Claim) -> None:
    run.provider = cmd.provider
    # If already cancelled here, RequestCancel's own DB write is still
    # queued *behind* this Claim (see Run.cancelled's docstring — the
    # flag is set synchronously, ahead of the queue, so this can observe
    # it true before _handle_cancel has actually run) — don't race it by
    # also writing "running" here; let that queued command own the DB
    # write, as it would for any other cancel.
    if not run.cancelled:
        async with souk.session() as session:
            await repo.mark_run_status(session, run.run_id, "running")
    # Hand the run over regardless of whether it's already cancelled.
    # open_run awaits the provider's own setup, so by the time this returns
    # a remote agent really has been sent its input — which matters because
    # the SDK's run task blocks waiting for exactly that (see
    # souk_agent_sdk.client._handle_run) and would otherwise sit there while
    # the cancel below tore everything down around it.
    stream = await open_run(cmd.provider, run.input_json)
    run.pump_task = asyncio.create_task(_pump(souk, run, stream))
    if run.cancelled:
        # broker.poll() already filters out cancelled runs before they're
        # ever handed to an agent — this only fires in the narrow window
        # where a run was cancelled *after* poll() handed it out but
        # *before* this claim arrived. Cancelling the pump closes the
        # stream, which is how the provider learns to stop; the queued
        # RequestCancel behind this Claim still runs next and does its own
        # (redundant but harmless) DB write.
        run.pump_task.cancel()


async def _handle_relay(souk: "Souk", run: Run, cmd: RelayEvent) -> None:
    # Persist *before* relaying: if souk crashes between the two, the
    # live caller must not end up having seen an event that was never
    # durably recorded.
    event = cmd.event
    interrupts = interrupt_outcome_of(event)
    if interrupts is not None:
        # Remembered for _handle_finish, which decides the run's final
        # status once the stream actually ends — an interrupt outcome
        # isn't itself a stream terminator (see souk/pause.py).
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
    # A run already cancelled (see _handle_cancel) reaching FinishStream
    # is the agent unwinding after being told to stop, not a real
    # completion — the DB status must stay "cancelled", not get
    # overwritten.
    if not run.cancelled:
        async with souk.session() as session:
            if run.pause_payload is not None:
                await repo.mark_run_status(session, run.run_id, "input-required", metadata=run.pause_payload)
            else:
                await repo.mark_run_status(session, run.run_id, "completed")
            # A genuine reply was produced (as opposed to failed/
            # cancelled — see souk/agui_reduce.py's module docstring for
            # why those don't go through this at all) — persist it as
            # real thread_history messages so souk is an actual source
            # of truth for the full conversation, not just the caller's
            # half of it. Only this round's own events (see
            # Run.round_starting_seq) — a resumed run's earlier round(s)
            # were already persisted the first time they finished.
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
    """The async half of a cancellation — see broker.request_cancel,
    which is what actually queues this and has already set
    `run.cancelled = True` synchronously before this ever runs (do not
    set it here too; some callers, e.g. _handle_claim's own narrow-race
    check, depend on observing it true *before* this handler has had a
    chance to run at all — see Run.cancelled's docstring). This handler
    is just the DB write and telling the agent side (if it already
    claimed this run) to stop producing further events, so it doesn't
    linger as 'running' until repo.fail_stalled_runs eventually sweeps
    it.

    Telling the agent to stop *is* cancelling the pump: that closes the
    provider's stream, which a remote transport turns back into whatever
    its own cancel signal is. The pump then pushes FinishStream on its way
    out, so this run still terminates through the same path a normally
    finished one does.

    If broker.poll() never handed this run to any agent at all, there's no
    pump and no provider to signal — broker._pipeline treats that
    combination as terminal and forgets the run right after this returns.
    """
    async with souk.session() as session:
        await repo.mark_run_status(session, run.run_id, "cancelled")
    if run.pump_task is not None:
        run.pump_task.cancel()
    await run.out_queue.put(END_OF_STREAM)


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
