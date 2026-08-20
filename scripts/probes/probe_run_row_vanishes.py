"""What funduq does when a run's row disappears underneath it mid-run.

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

funduq told the caller a complete story about a run the database had never heard
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

    cd funduq && uv run python ../scripts/probes/probe_run_row_vanishes.py
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

from funduq import repo
from funduq.config import CoreSettings
from funduq.core import Funduq
from funduq.identity import provider_connect_signing_payload, registration_signing_payload
from funduq.schema import agents, run_events, runs, thread_messages, threads

DB = Path(tempfile.gettempdir()) / "funduq_probe_vanish.db"
URL = f"sqlite+aiosqlite:///{DB}"


def migrate() -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB) + suffix)
        if p.exists():
            p.unlink()
    os.environ["FUNDUQ_DATABASE_URL"] = URL
    cfg = Config()
    cfg.set_main_option("script_location", "funduq:alembic")
    command.upgrade(cfg, "head")


class _Taker:
    """Takes the run and does nothing else, so it is live and owned while the
    database is pulled out from under it. The events below are pushed by hand
    because this probe is about what funduq does with them, not about an agent.
    """

    def __init__(self, key, public_key: str) -> None:
        self._key = key
        self.public_key = public_key
        self.max_concurrent_runs = None

    async def deliver(self, run) -> bool:
        return True

    def cancel(self, run_id: str) -> None:
        pass

    def sign_connect(
        self, funduq_public_key: str, funduq_nonce: str, provider_nonce: str, names: list[str]
    ) -> str:
        return self._key.sign(
            provider_connect_signing_payload(funduq_public_key, funduq_nonce, provider_nonce, names)
        ).hex()


async def main() -> int:
    migrate()
    funduq = Funduq(CoreSettings(database_url=URL, token_signing_secret="probe"))
    await funduq.start()
    key = Ed25519PrivateKey.generate()
    public_key = key.public_key().public_bytes_raw().hex()
    timestamp = int(time.time())
    registration = await funduq.register_agents(
        public_key,
        key.sign(registration_signing_payload(["a"], timestamp)).hex(),
        timestamp,
        [{"name": "a"}],
    )
    agent = registration.agents["a"]

    await funduq.attach_provider(_Taker(key, public_key), ["a"])
    handle = await funduq.start_run(agent, {"messages": []})
    async with asyncio.timeout(5):
        while funduq.broker.get(handle.run_id).claimed_by is None:
            await asyncio.sleep(0)

    # The database is replaced underneath a live run: a restore, or a funduq
    # pointed at a fresh database while a provider's connection stayed open.
    async with funduq.session() as session:
        for table in (run_events, thread_messages, runs, threads, agents):
            await session.execute(delete(table))
        await session.commit()
    print("wiped the database while the run is live")

    accepted = funduq.report_event(
        handle.run_id,
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m", "delta": "hi"},
        claimed_by=public_key,
    )
    funduq.finish_run(handle.run_id, claimed_by=public_key)

    # A terminated stream is itself part of the check: a handler raising must
    # not strand whoever is watching.
    try:
        async with asyncio.timeout(10):
            events = [event async for event in handle.events()]
        hung = False
    except TimeoutError:
        events, hung = [], True

    await asyncio.sleep(0.3)
    async with funduq.session() as session:
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
    await funduq.aclose()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
