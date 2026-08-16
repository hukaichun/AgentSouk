"""Detects providers that took a run and then went silent, plus
(optionally) runs paused on a human who never came back.

Deliberately narrow scope for the provider-facing sweep: a run sitting
'queued' isn't a health signal here — nobody has taken it yet, and the
broker gives up on those itself, from memory, without a database read
(RunBroker.expire_queued). Only a run a provider acked (status='running')
and then produced no further activity for too long counts as a problem
this sweep can see — see repo.fail_stalled_runs. A separate, opt-in sweep (repo.fail_stale_paused_runs,
gated on settings.paused_timeout_seconds) covers 'input-required' runs —
waiting on a human has no generally-correct timeout, so unlike the other
two sweeps it's disabled (None) by default.

"How to report this" is intentionally left minimal for now: a structured
log line, plus the run's own status/metadata becoming queryable (via
tasks/get or a direct query) — no webhooks/alerting yet, that's a
separate design.
"""

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
    """Unblocks whoever's still waiting on this run's output (an open AG-UI
    SSE connection or an A2A tasks/sendSubscribe stream) — otherwise they'd
    hang until their own client-side timeout with no idea the run had
    already been given up on. Pushing Fail (see handlers._handle_fail)
    persists an explicit RUN_ERROR event before closing the stream, so a
    live subscriber gets a real terminal signal — protocols.a2a_translate's
    agui_event_to_a2a_update already maps RUN_ERROR to a final
    `status: failed` update, so this serves AG-UI and A2A callers alike.

    A no-op if the broker is no longer dispatching the run (finished, or
    already cancelled) — this is a command push, not a mutation, and the
    broker answers whether it landed.
    """
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
