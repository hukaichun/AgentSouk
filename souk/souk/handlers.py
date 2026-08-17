from __future__ import annotations

import contextlib
import logging
from functools import partial
from typing import TYPE_CHECKING

from ag_ui.core import Event
from pydantic import TypeAdapter, ValidationError

from souk import repo
from souk.agui_reduce import reduce_events_to_messages
from souk.broker import (
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

_EVENT = TypeAdapter(Event)


async def _handle_claim(souk: "Souk", run: Run, cmd: Claim) -> None:
    async with souk.session() as session:
        await souk.mark_run_status(session, run.run_id, "running")


async def _handle_relay(souk: "Souk", run: Run, cmd: RelayEvent) -> None:
    event = cmd.event
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
        souk.broker.push(run.run_id, Fail("provider sent a malformed AG-UI event"))
        return
    if event.get("type") == "RUN_FINISHED":
        run.saw_run_finished = True
        interrupts = interrupt_outcome_of(event)
        if interrupts is not None:
            run.pause_payload = {"interrupts": interrupts}
    elif event.get("type") == "RUN_ERROR":
        run.saw_run_error = True
    run.seq += 1
    async with souk.session() as session:
        await repo.append_run_event(session, run.run_id, run.seq, event)
        await repo.touch_run_activity(session, run.run_id)
        await session.commit()
    await run.out_queue.put(event)


async def _handle_finish(souk: "Souk", run: Run, cmd: FinishStream) -> None:
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

    async with souk.session() as session:
        await souk.mark_run_status(session, run.run_id, status, metadata=metadata)
        if status in ("completed", "input-required"):
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


async def _handle_cancel(souk: "Souk", run: Run, cmd: RequestCancel) -> None:
    if run.claimed_by is None:
        async with souk.session() as session:
            await souk.mark_run_status(session, run.run_id, "cancelled")
        return

    async with souk.session() as session:
        await souk.mark_run_status(session, run.run_id, "cancelling")
    if run.cancel_notify is not None:
        with contextlib.suppress(Exception):
            run.cancel_notify(run.run_id)


async def _handle_fail(souk: "Souk", run: Run, cmd: Fail) -> None:
    event = {"type": "RUN_ERROR", "message": cmd.reason}
    run.seq += 1
    async with souk.session() as session:
        await repo.append_run_event(session, run.run_id, run.seq, event)
        await souk.mark_run_status(session, run.run_id, "failed", metadata={"failureReason": cmd.reason})
    await run.out_queue.put(event)


def make_handlers(souk: "Souk") -> HandlerMap:
    return {
        Claim: partial(_handle_claim, souk),
        RelayEvent: partial(_handle_relay, souk),
        FinishStream: partial(_handle_finish, souk),
        RequestCancel: partial(_handle_cancel, souk),
        Fail: partial(_handle_fail, souk),
    }
