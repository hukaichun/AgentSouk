"""One souk process, reachable over a unix socket. A probe fixture, not a gateway.

The horizontal-scaling work (design/broker-horizontal-scaling.md) is about what
happens when several souk processes share one database, and CLAUDE.md's rule
is that this gets found by running something rather than by reading. Running
it needs two things souk itself deliberately does not have: a way to reach a
souk from outside its process, and something to spread calls across several of
them.

This is the first. It is the smallest possible stand-in for a serving layer:
newline-delimited JSON over a unix socket, one request per connection, no
framework, no HTTP, no auth of its own. It exists to be crude — every real
decision it might make (which port, which framework, how to authenticate) is
the gateway's to make in its own repository, and making any of them here would
be this repo growing a serving layer under a different name.

What it must be honest about is the domain: every op below is a plain call
into `Souk`, with no shortcut past `claim_work`'s identity checks or
`report_event`'s ownership check. A probe that cheated on those would prove
nothing about the thing being probed.

    python scripts/probes/node.py --name a --socket /tmp/souk-a.sock

`SOUK_DATABASE_URL` and `SOUK_TOKEN_SIGNING_SECRET` come from the environment,
as they would anywhere else. Every node in a cluster must share both — the
database because that is the whole point, the secret because a session token
minted by one node is verified by whichever node the next call lands on.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
from typing import Any

from souk import repo
from souk.config import CoreSettings
from souk.core import Souk
from souk.models import AgentRef

logger = logging.getLogger("probe.node")


def _agent(req: dict[str, Any]) -> AgentRef:
    """An agent named the way souk names one — the pair, off the wire."""
    return AgentRef(provider_key=req["provider_key"], name=req["agent_name"])


async def _dispatch(souk: Souk, req: dict[str, Any]) -> Any:
    """One domain call. Deliberately a flat table of `Souk`'s own methods —
    if an op here needed logic of its own, that logic would be a serving-layer
    decision this probe has no business making.
    """
    op = req["op"]

    if op == "souk_start":
        return {"reaped": await souk.start()}

    if op == "register":
        registration = await souk.register_agents(
            req["public_key"], req["signature"], req["timestamp"], req["agents"]
        )
        return {
            # The pairs, indexed by name. Not ids: souk mints none, which is
            # the point of retiring the surrogate id.
            "agents": {
                name: {"provider_key": ref.provider_key, "agent_name": ref.name}
                for name, ref in registration.agents.items()
            },
            "session_token": registration.session_token,
        }

    if op == "start_run":
        handle = await souk.start_run(_agent(req), req["run_input"])
        return {"run_id": handle.run_id, "thread_id": handle.thread_id, "is_live": handle.is_live}

    if op == "claim_work":
        claimed = await souk.claim_work(
            req["token"],
            req["agent_names"],
            max_claim=req.get("max_claim"),
            wait_seconds=req.get("wait_seconds", 0),
        )
        return [
            {
                "run_id": run.run_id,
                "provider_key": run.agent.provider_key,
                "agent_name": run.agent.name,
                "thread_id": run.thread_id,
                "run_input": run.run_input,
            }
            for run in claimed
        ]

    if op == "report_event":
        return souk.report_event(req["run_id"], req["event"], claimed_by=req["claimed_by"])

    if op == "finish_run":
        return souk.finish_run(req["run_id"], claimed_by=req["claimed_by"])

    if op == "cancel_run":
        return souk.cancel_run(req["run_id"])

    if op == "get_run":
        record = await souk.get_run(req["run_id"])
        return record.model_dump(mode="json") if record is not None else None

    if op == "get_run_events":
        return await souk.get_run_events(req["run_id"])

    if op == "active_runs":
        return souk.active_runs()

    if op == "list_agents":
        return [agent.model_dump(mode="json") for agent in await souk.list_agents()]

    if op == "sweep_once":
        # The health sweep on demand, so a probe can provoke it instead of
        # waiting out health_sweep_interval_seconds.
        from souk.health import sweep_once

        await sweep_once(souk)
        return True

    if op == "touch_agent":
        async with souk.session() as session:
            await repo.touch_agents(session, req["provider_key"], [req["agent_name"]])
        return True

    raise ValueError(f"unknown op: {op}")


async def _handle(souk: Souk, name: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    async def send(obj: Any) -> None:
        writer.write((json.dumps(obj) + "\n").encode())
        await writer.drain()

    try:
        line = await reader.readline()
        if not line:
            return
        req = json.loads(line)

        # `subscribe` is the one streaming op: it answers with an
        # {"event": …} line per event and one {"end": true}, then closes.
        # Whether it produces anything at all is itself a probe result — a
        # node that is not dispatching the run answers with an immediately
        # empty stream (see RunBroker.subscribe), which is precisely the
        # cross-node read problem the design has to solve.
        if req.get("op") == "subscribe":
            await send({"ok": True, "node": name, "streaming": True})
            async for event in souk.broker.subscribe(req["run_id"]):
                await send({"event": event})
            await send({"end": True})
            return

        await send({"ok": True, "node": name, "result": await _dispatch(souk, req)})
    except Exception as exc:
        logger.exception("op failed")
        with contextlib.suppress(Exception):
            await send({"ok": False, "node": name, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="this node's label in probe output")
    parser.add_argument("--socket", required=True, help="unix socket path to listen on")
    parser.add_argument(
        "--start",
        action="store_true",
        help="call Souk.start() on boot (orphan reconciliation + health sweeps), as a "
        "real deployment does. Off by default so a probe can choose when that happens — "
        "which node boots when is exactly what the design is about.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format=f"%(levelname)s [{args.name}] %(name)s: %(message)s"
    )

    souk = Souk(CoreSettings())
    if args.start:
        await souk.start()

    with contextlib.suppress(FileNotFoundError):
        os.unlink(args.socket)
    server = await asyncio.start_unix_server(
        lambda r, w: _handle(souk, args.name, r, w), path=args.socket
    )

    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)

    # Printed, not logged: the harness waits for this line to know the socket
    # is accepting rather than sleeping and hoping.
    print(f"READY {args.name} {args.socket}", flush=True)

    async with server:
        await stopped.wait()

    await souk.aclose()
    with contextlib.suppress(FileNotFoundError):
        os.unlink(args.socket)


if __name__ == "__main__":
    asyncio.run(main())
