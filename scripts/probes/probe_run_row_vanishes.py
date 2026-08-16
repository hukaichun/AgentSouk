"""What souk does when a run's row disappears underneath it mid-run.

The acceptance check for splitting `thread_history` into `runs` and
`thread_messages`. Before the split, `run_events.run_id` could not be a
foreign key — the column it needed to reference was not unique in the merged
table — and its comment said the integrity was "enforced at the application
layer instead". Nothing enforced it. This is what that measured:

    wiped the database while the run is live
      report_event      -> True
      caller's stream   -> 2 event(s): ['TEXT_MESSAGE_CONTENT', 'RUN_ERROR']
      run in database   -> None
      run_events rows   -> 2   ← orphans, belonging to a run that never existed

souk told the caller a complete story about a run the database had never heard
of, recorded nothing, and did not complain anywhere.

With the split the reference is a real foreign key, so the write fails and is
logged, and the run's stream terminates rather than hanging (see
broker._pipeline, which sends END_OF_STREAM itself precisely because a handler
that raises would otherwise skip its own). Expected now:

      report_event      -> True    (the push is accepted; persistence is what fails)
      caller's stream   -> 0 event(s), terminated
      run in database   -> None
      run_events rows   -> 0

Nothing is relayed because `_handle_relay` persists before relaying, which is
the rule that keeps a caller from seeing an event that was never recorded.

    cd souk && uv run python ../scripts/probes/probe_run_row_vanishes.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import delete

from souk import repo
from souk.config import CoreSettings
from souk.core import Souk
from souk.identity import registration_signing_payload
from souk.schema import agents, run_events, runs, thread_messages, threads

DB = Path(tempfile.gettempdir()) / "souk_probe_vanish.db"
URL = f"sqlite+aiosqlite:///{DB}"


def migrate() -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB) + suffix)
        if p.exists():
            p.unlink()
    os.environ["SOUK_DATABASE_URL"] = URL
    cfg = Config(str(Path("alembic.ini").resolve()))
    cfg.set_main_option("script_location", str(Path("alembic").resolve()))
    command.upgrade(cfg, "head")


async def main() -> int:
    migrate()
    souk = Souk(CoreSettings(database_url=URL, token_signing_secret="probe"))
    key = Ed25519PrivateKey.generate()
    public_key = key.public_key().public_bytes_raw().hex()
    timestamp = int(time.time())
    registration = await souk.register_agents(
        public_key,
        key.sign(registration_signing_payload(["a"], timestamp)).hex(),
        timestamp,
        [{"name": "a"}],
    )
    agent = registration.agents["a"]

    handle = await souk.start_run(agent, {"messages": []})
    await souk.claim_work(registration.session_token, ["a"])
    await asyncio.sleep(0.2)

    # The database is replaced underneath a live run: a restore, or a souk
    # pointed at a fresh database while a provider's connection stayed open.
    async with souk.session() as session:
        for table in (run_events, thread_messages, runs, threads, agents):
            await session.execute(delete(table))
        await session.commit()
    print("wiped the database while the run is live")

    accepted = souk.report_event(
        handle.run_id,
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m", "delta": "hi"},
        claimed_by=public_key,
    )
    souk.finish_run(handle.run_id, claimed_by=public_key)

    # A terminated stream is itself part of the check: a handler raising must
    # not strand whoever is watching.
    try:
        async with asyncio.timeout(10):
            events = [event async for event in handle.events()]
        hung = False
    except TimeoutError:
        events, hung = [], True

    await asyncio.sleep(0.3)
    async with souk.session() as session:
        stored_run = await repo.get_run(session, handle.run_id)
        stored_events = await repo.get_run_events(session, handle.run_id)

    print(f"  report_event      -> {accepted}")
    print(f"  caller's stream   -> {len(events)} event(s), {'HUNG' if hung else 'terminated'}")
    print(f"  run in database   -> {stored_run}")
    print(f"  run_events rows   -> {len(stored_events)}")

    ok = not hung and not stored_events
    print(
        "\nOK   nothing was written for a run the database does not have, and the "
        "stream ended" if ok
        else "\nBROKEN: " + ("the stream hung" if hung else f"{len(stored_events)} orphan row(s) written")
    )
    await souk.aclose()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
