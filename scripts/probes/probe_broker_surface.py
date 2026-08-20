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

from funduq.broker import ConnectedProvider, Run, RunBroker, RunSnapshot
from funduq.config import CoreSettings
from funduq.core import Funduq
from funduq.identity import registration_signing_payload

DB = Path(tempfile.gettempdir()) / "funduq_probe_surface.db"
URL = f"sqlite+aiosqlite:///{DB}"


def migrate() -> None:
    import os

    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB) + suffix)
        if p.exists():
            p.unlink()
    os.environ["FUNDUQ_DATABASE_URL"] = URL
    cfg = Config()
    cfg.set_main_option("script_location", "funduq:alembic")
    command.upgrade(cfg, "head")


async def main() -> None:
    migrate()
    funduq = Funduq(CoreSettings(database_url=URL, token_signing_secret="probe"))
    await funduq.start()

    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes_raw().hex()
    ts = int(time.time())
    sig = key.sign(registration_signing_payload(["prober"], ts)).hex()
    reg = await funduq.register_agents(pub, sig, ts, [{"name": "prober"}])
    agent = reg.agents["prober"]

    handle = await funduq.start_run(agent, {"messages": [], "state": {}})

    print("--- what escapes the broker ---")
    returned = funduq.enqueue_run("run_probe", agent, handle.thread_id, {}, "ag-ui")
    print(f"Funduq.enqueue_run annotated -> {inspect.signature(Funduq.enqueue_run).return_annotation}")
    print(f"Funduq.enqueue_run actually returns -> {type(returned).__name__}")
    print(f"  is a live Run (queues attached): {isinstance(returned, Run)}")
    if isinstance(returned, Run):
        print(f"  caller can reach: in_queue={type(returned.in_queue).__name__}, "
              f"out_queue={type(returned.out_queue).__name__}")
        print(f"  and can mutate fields directly, e.g. claimed_by = {returned.claimed_by!r}")

    print("\n--- what crosses to a provider ---")
    print(f"ConnectedProvider.deliver takes -> "
          f"{inspect.signature(ConnectedProvider.deliver).parameters['run'].annotation}")
    print(f"  is that broker.Run?  {ConnectedProvider.deliver.__annotations__.get('run') is Run}")
    print("  (it must not be: a provider holding a Run holds funduq's own queues,")
    print("   and a Run cannot be handed across a wire at all)")

    print("\n--- reachability ---")
    print(f"RunBroker.serving -> {inspect.signature(RunBroker.serving).return_annotation}")
    print(f"  provider mapping is private: {not hasattr(funduq.broker, 'providers')}")

    print("\n--- sync vs async on the surface ---")
    for name in ("enqueue_run", "get", "push", "subscribe", "request_cancel",
                 "serving", "agents_served_by", "active_run_ids", "quality", "forget"):
        method = getattr(RunBroker, name)
        kind = "async" if inspect.iscoroutinefunction(method) else "sync "
        print(f"  {kind} {name}")

    await funduq.aclose()


asyncio.run(main())
