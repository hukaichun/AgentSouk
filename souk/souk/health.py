"""Detects providers that claimed a run and then went silent.

Deliberately narrow scope: a run sitting 'queued' isn't a health signal
by itself — a provider is expected to throttle how much it claims via
PollRequest.max_claim, so backlog is normal, self-imposed pacing, not an
anomaly. Only a run a provider explicitly claimed (status='running') and
then produced no further activity for too long counts as a real problem —
see repo.fail_stalled_runs.

"How to report this" is intentionally left minimal for now: a structured
log line, plus the run's own status/metadata becoming queryable (via
tasks/get or a direct query) — no webhooks/alerting yet, that's a
separate design.
"""

from __future__ import annotations

import asyncio
import logging

from souk import repo
from souk.broker import broker
from souk.config import settings
from souk.db import SessionLocal

logger = logging.getLogger("souk.health")


async def sweep_once() -> None:
    async with SessionLocal() as session:
        stalled = await repo.fail_stalled_runs(session, settings.run_stall_timeout_seconds)
    for run_id in stalled:
        # Unblocks whoever's still waiting on this run's output (an open
        # AG-UI SSE connection or an A2A tasks/sendSubscribe stream) —
        # otherwise they'd hang until their own client-side timeout with
        # no idea the run had already been given up on.
        await broker.close_run(run_id)
    if stalled:
        logger.warning(
            "health sweep: %d run(s) claimed but silent past %ds, marked failed: %s",
            len(stalled),
            settings.run_stall_timeout_seconds,
            stalled,
        )


async def run_health_sweeps_forever() -> None:
    while True:
        await asyncio.sleep(settings.health_sweep_interval_seconds)
        try:
            await sweep_once()
        except Exception:
            logger.exception("health sweep failed")
