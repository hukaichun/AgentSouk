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


async def _close_with_terminal_event(run_id: str, failure_reason: str) -> None:
    """Unblocks whoever's still waiting on this run's output (an open AG-UI
    SSE connection or an A2A tasks/sendSubscribe stream) — otherwise they'd
    hang until their own client-side timeout with no idea the run had
    already been given up on. Pushes an explicit RUN_ERROR event before
    closing (rather than just closing the stream) so a live subscriber gets
    a real terminal signal — translate_a2a.agui_event_to_a2a_update already
    maps RUN_ERROR to a final `status: failed` update, so this serves AG-UI
    and A2A callers alike.
    """
    event = {"type": "RUN_ERROR", "message": failure_reason}
    if broker.get(run_id) is not None:
        async with SessionLocal() as session:
            seq = broker.next_seq(run_id)
            await repo.append_run_event(session, run_id, seq, event)
        await broker.deliver_event(run_id, event)
    await broker.close_run(run_id)


async def sweep_once() -> None:
    async with SessionLocal() as session:
        stalled = await repo.fail_stalled_runs(session, settings.run_stall_timeout_seconds)
        unclaimed = await repo.fail_unclaimed_runs(session, settings.queued_timeout_seconds)
    for run_id in stalled:
        await _close_with_terminal_event(run_id, "stalled_no_activity")
    if stalled:
        logger.warning(
            "health sweep: %d run(s) claimed but silent past %ds, marked failed: %s",
            len(stalled),
            settings.run_stall_timeout_seconds,
            stalled,
        )
    for run_id in unclaimed:
        await _close_with_terminal_event(run_id, "no_provider_online")
    if unclaimed:
        logger.warning(
            "health sweep: %d run(s) queued past %ds with target agent offline, marked failed: %s",
            len(unclaimed),
            settings.queued_timeout_seconds,
            unclaimed,
        )


async def run_health_sweeps_forever() -> None:
    while True:
        await asyncio.sleep(settings.health_sweep_interval_seconds)
        try:
            await sweep_once()
        except Exception:
            logger.exception("health sweep failed")
