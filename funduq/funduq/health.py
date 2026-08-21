from __future__ import annotations

import asyncio
import logging

from typing import TYPE_CHECKING

from funduq import repo
from funduq.broker import Fail

if TYPE_CHECKING:
    from funduq.core import Funduq

logger = logging.getLogger("funduq.health")


async def close_with_terminal_event(funduq: "Funduq", run_id: str, failure_reason: str) -> None:
    """Give a run funduq has just failed its terminal `RUN_ERROR`, whether or not
    the broker still tracks it.

    A live run gets a `Fail` pushed into its pipeline, which appends the event
    and relays it to any subscriber. A run the broker has already forgotten —
    a stale pause reaped by the sweep, an orphan reaped at startup — has no
    pipeline and no subscriber left, but the record still owes the verdict: the
    same event is appended directly, so the event stream ends the way the
    database says the run did. A run that already carries its own `RUN_ERROR`
    is left alone, same as everywhere else."""
    if funduq.broker.push(run_id, Fail(failure_reason)):
        return
    async with funduq.session() as session:
        events = await repo.get_run_events(session, run_id)
        if any(e.get("type") == "RUN_ERROR" for e in events):
            return
        seq = await repo.get_last_event_seq(session, run_id) + 1
        await repo.append_run_event(
            session, run_id, seq, {"type": "RUN_ERROR", "message": failure_reason}
        )
        await session.commit()


async def sweep_once(funduq: "Funduq") -> None:
    """Fails runs that have gone silent (claimed but no activity) past
    `run_stall_timeout_seconds`. Also fails runs stuck paused
    (input-required) past `paused_timeout_seconds`, but only if that setting
    is configured — it's skipped entirely when it's None."""
    settings = funduq.settings
    async with funduq.session() as session:
        stalled = await repo.fail_stalled_runs(session, settings.run_stall_timeout_seconds)
        stale_paused: list[str] = []
        if settings.paused_timeout_seconds is not None:
            stale_paused = await repo.fail_stale_paused_runs(session, settings.paused_timeout_seconds)
    for run_id in stalled:
        await close_with_terminal_event(funduq, run_id, "stalled_no_activity")
    if stalled:
        logger.warning(
            "health sweep: %d run(s) claimed but silent past %ds, marked failed: %s",
            len(stalled),
            settings.run_stall_timeout_seconds,
            stalled,
        )
    for run_id in stale_paused:
        await close_with_terminal_event(funduq, run_id, "paused_no_resume")
    if stale_paused:
        logger.warning(
            "health sweep: %d run(s) paused (input-required) past %ds with no resume, marked failed: %s",
            len(stale_paused),
            settings.paused_timeout_seconds,
            stale_paused,
        )


async def run_health_sweeps_forever(funduq: "Funduq") -> None:
    while True:
        await asyncio.sleep(funduq.settings.health_sweep_interval_seconds)
        try:
            await sweep_once(funduq)
        except Exception:
            logger.exception("health sweep failed")
