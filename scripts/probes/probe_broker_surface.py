"""What does the broker actually hand out, versus what the annotations say?

Reading produced a confident answer twice in this repo already. This runs it.
"""

import asyncio
import inspect
import tempfile
import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from souk.broker import Run, RunBroker, RunSnapshot
from souk.config import CoreSettings
from souk.core import Souk
from souk.identity import registration_signing_payload

DB = Path(tempfile.gettempdir()) / "souk_probe_surface.db"
URL = f"sqlite+aiosqlite:///{DB}"


def migrate() -> None:
    import os

    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB) + suffix)
        if p.exists():
            p.unlink()
    os.environ["SOUK_DATABASE_URL"] = URL
    cfg = Config(str(Path("alembic.ini").resolve()))
    cfg.set_main_option("script_location", str(Path("alembic").resolve()))
    command.upgrade(cfg, "head")


async def main() -> None:
    migrate()
    souk = Souk(CoreSettings(database_url=URL, token_signing_secret="probe"))

    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes_raw().hex()
    ts = int(time.time())
    sig = key.sign(registration_signing_payload(["prober"], ts)).hex()
    reg = await souk.register_agents(pub, sig, ts, [{"name": "prober"}])
    agent_id = reg.agent_ids["prober"]

    handle = await souk.start_run(agent_id, {"messages": [], "state": {}})

    print("--- what escapes the broker ---")
    returned = souk.enqueue_run("run_probe", agent_id, handle.thread_id, {}, "ag-ui")
    print(f"Souk.enqueue_run annotated -> {inspect.signature(Souk.enqueue_run).return_annotation}")
    print(f"Souk.enqueue_run actually returns -> {type(returned).__name__}")
    print(f"  is a live Run (queues attached): {isinstance(returned, Run)}")
    if isinstance(returned, Run):
        print(f"  caller can reach: in_queue={type(returned.in_queue).__name__}, "
              f"out_queue={type(returned.out_queue).__name__}")
        print(f"  and can mutate fields directly, e.g. claimed_by = {returned.claimed_by!r}")

    print("\n--- claim ---")
    claimed = souk.broker.claim([agent_id], claimed_by=pub, max_claim=1)
    print(f"RunBroker.claim -> list[{type(claimed[0]).__name__ if claimed else '?'}]")

    print("\n--- the wake seam ---")
    event = souk.broker.subscribe_wake([agent_id])
    print(f"RunBroker.subscribe_wake -> {type(event).__name__} "
          f"({type(event).__module__}) — core awaits .wait() on this directly")
    souk.broker.unsubscribe_wake([agent_id], event)

    print("\n--- sync vs async on the surface ---")
    for name in ("enqueue_run", "claim", "get", "push", "subscribe", "request_cancel",
                 "active_run_ids", "subscribe_wake", "forget"):
        method = getattr(RunBroker, name)
        kind = "async" if inspect.iscoroutinefunction(method) else "sync "
        print(f"  {kind} {name}")

    await souk.aclose()


asyncio.run(main())
