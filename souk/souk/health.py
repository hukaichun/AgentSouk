from __future__ import annotations

import asyncio
import logging

from typing import TYPE_CHECKING

from souk import repo
from souk.broker import Fail

if TYPE_CHECKING:
    from souk.core import Souk

logger = logging.getLogger("souk.health")


async def _close_with_terminal_event(souk: "Souk", run_id: str, failure_reason: str) -> None:
    souk.broker.push(run_id, Fail(failure_reason))


async def sweep_once(souk: "Souk") -> None:
    settings = souk.settings
    async with souk.session() as session:
        stalled = await repo.fail_stalled_runs(session, settings.run_stall_timeout_seconds)
        stale_paused: list[str] = []
        if settings.paused_timeout_seconds is not None:
            stale_paused = await repo.fail_stale_paused_runs(session, settings.paused_timeout_seconds)
    for run_id in stalled:
        await _close_with_terminal_event(souk, run_id, "stalled_no_activity")
    if stalled:
        logger.warning(
            "health sweep: %d run(s) claimed but silent past %ds, marked failed: %s",
            len(stalled),
            settings.run_stall_timeout_seconds,
            stalled,
        )
    for run_id in stale_paused:
        await _close_with_terminal_event(souk, run_id, "paused_no_resume")
    if stale_paused:
        logger.warning(
            "health sweep: %d run(s) paused (input-required) past %ds with no resume, marked failed: %s",
            len(stale_paused),
            settings.paused_timeout_seconds,
            stale_paused,
        )


async def run_health_sweeps_forever(souk: "Souk") -> None:
    while True:
        await asyncio.sleep(souk.settings.health_sweep_interval_seconds)
        try:
            await sweep_once(souk)
        except Exception:
            logger.exception("health sweep failed")
