from __future__ import annotations

import contextlib
import logging
from functools import partial
from typing import TYPE_CHECKING

from ag_ui.core import Event, EventType
from pydantic import TypeAdapter, ValidationError

from funduq import repo
from funduq.agui_reduce import reduce_events_to_messages
from funduq.broker import (
    Claim,
    Fail,
    FinishStream,
    HandlerMap,
    RelayEvent,
    RequestCancel,
    Run,
)
from funduq.pause import interrupt_outcome_of

if TYPE_CHECKING:
    from funduq.core import Funduq

logger = logging.getLogger("funduq.handlers")

_EVENT = TypeAdapter(Event)
_KNOWN_EVENT_TYPES = frozenset(member.value for member in EventType)


async def _handle_claim(funduq: "Funduq", run: Run, cmd: Claim) -> None:
    """Mark the run "running" once a provider has claimed it."""
    async with funduq.session() as session:
        await funduq.mark_run_status(session, run.run_id, "running")


async def _handle_relay(funduq: "Funduq", run: Run, cmd: RelayEvent) -> None:
    """Validate, persist, and forward a provider event; fail the run on invalid AG-UI.

    Three-way rule. An event whose `type` is one funduq's pinned AG-UI knows is
    validated strictly, and a validation failure drains the run's queue and
    pushes a `Fail` — as does an event with no `type` string at all: both are
    malformation, not version skew. An event whose `type` is a string funduq
    does not recognise is a newer AG-UI's event, and it is relayed untouched:
    funduq stores and forwards it and never branches on its content — whether to
    skip it is the caller's decision (AG-UI's fail-open rule), never the
    relay's. A `RUN_FINISHED` carrying an interrupt outcome records it as the
    run's pause payload; a `RUN_ERROR` is remembered so `_handle_finish`
    doesn't synthesize a second one.
    """
    event = cmd.event
    type_tag = event.get("type") if isinstance(event, dict) else None
    if not isinstance(type_tag, str) or type_tag in _KNOWN_EVENT_TYPES:
        try:
            _EVENT.validate_python(event)
        except ValidationError as e:
            logger.warning(
                "run %s: provider sent an event that is not valid AG-UI, ending the run: %s",
                run.run_id,
                e,
            )
            while not run.in_queue.empty():
                run.in_queue.get_nowait()
            funduq.broker.push(run.run_id, Fail("provider sent a malformed AG-UI event"))
            return
    if event.get("type") == "RUN_FINISHED":
        run.saw_run_finished = True
        interrupts = interrupt_outcome_of(event)
        if interrupts is not None:
            run.pause_payload = {"interrupts": interrupts}
    elif event.get("type") == "RUN_ERROR":
        run.saw_run_error = True
    run.seq += 1
    async with funduq.session() as session:
        await repo.append_run_event(session, run.run_id, run.seq, event)
        await repo.touch_run_activity(session, run.run_id)
        await session.commit()
    await run.out_queue.put(event)


async def _handle_finish(funduq: "Funduq", run: Run, cmd: FinishStream) -> None:
    """Settle the run's final status and, when it completed or paused, fold its events into thread messages.

    Status is "input-required" if the run paused, "completed" if it saw `RUN_FINISHED`,
    "cancelled" if a cancel was requested, else "failed". A run that ends failed without
    ever having reported its own `RUN_ERROR` gets one synthesized and appended.
    """
    if run.pause_payload is not None:
        status, metadata = "input-required", run.pause_payload
    elif run.saw_run_finished:
        status, metadata = "completed", None
    elif run.cancel_requested:
        status, metadata = "cancelled", None
    else:
        status, metadata = "failed", {"failureReason": "provider_stream_ended_without_finishing"}

    failure_event = (
        {
            "type": "RUN_ERROR",
            "message": "the agent's stream ended without finishing",
            "code": "provider_stream_ended_without_finishing",
        }
        if status == "failed" and not run.saw_run_error
        else None
    )

    async with funduq.session() as session:
        settled = await funduq.mark_run_status(session, run.run_id, status, metadata=metadata)
        if settled and status in ("completed", "input-required"):
            round_events = await repo.get_run_events(session, run.run_id, since_seq=run.round_starting_seq)
            reply_messages = reduce_events_to_messages(round_events)
            if reply_messages:
                await repo.append_thread_messages(session, run.thread_id, run.run_id, reply_messages)
        if failure_event is not None:
            run.seq += 1
            await repo.append_run_event(session, run.run_id, run.seq, failure_event)
        await session.commit()
    if failure_event is not None:
        await run.out_queue.put(failure_event)


async def _handle_cancel(funduq: "Funduq", run: Run, cmd: RequestCancel) -> None:
    """Cancel immediately if no provider has claimed the run yet; otherwise ask the provider to stop.

    A claimed run is marked "cancelling" (not "cancelled") and its `cancel_notify`
    callback is invoked — funduq asks the provider to stop, it does not decide the outcome
    on its behalf.
    """
    if run.claimed_by is None:
        async with funduq.session() as session:
            await funduq.mark_run_status(session, run.run_id, "cancelled")
        return

    async with funduq.session() as session:
        await funduq.mark_run_status(session, run.run_id, "cancelling")
    if run.cancel_notify is not None:
        with contextlib.suppress(Exception):
            run.cancel_notify(run.run_id)


async def _handle_fail(funduq: "Funduq", run: Run, cmd: Fail) -> None:
    """Append a `RUN_ERROR` event carrying `cmd.reason` and mark the run failed with that reason."""
    event = {"type": "RUN_ERROR", "message": cmd.reason}
    run.seq += 1
    async with funduq.session() as session:
        await repo.append_run_event(session, run.run_id, run.seq, event)
        await funduq.mark_run_status(session, run.run_id, "failed", metadata={"failureReason": cmd.reason})
    await run.out_queue.put(event)


def make_handlers(funduq: "Funduq") -> HandlerMap:
    """Build the broker's command-type-to-handler map, bound to this `funduq` instance."""
    return {
        Claim: partial(_handle_claim, funduq),
        RelayEvent: partial(_handle_relay, funduq),
        FinishStream: partial(_handle_finish, funduq),
        RequestCancel: partial(_handle_cancel, funduq),
        Fail: partial(_handle_fail, funduq),
    }
