"""N funduq processes and a hand-rolled load balancer over them.

The second half of the probe fixture (see node.py for the first). A real
deployment puts nginx or a cloud LB in front of several funduq processes; this
is fifty lines that do the part that matters for finding bugs — send each call
to a different process — without introducing a dependency, a config file or a
second thing to install.

**Deliberately harsher than a real load balancer.** This round-robins *every
single call*, with no session affinity and no stickiness of any kind: the call
that starts a run, the worker's claim for it, each event that worker reports,
and the caller's read of the result can all land on four different processes.
Real load balancers are usually kinder than that, and kindness is what makes
this class of bug show up in production six months late instead of here. When
a probe needs a specific node — "boot B *now*, while A is holding a run" — it
asks for one by name (`call(..., node="b")`), because that is a scenario, not
a routing decision.

The processes are real: separate OS processes, spawned with `subprocess`, each
constructing its own `Funduq` with its own engine and its own in-memory broker,
sharing only `FUNDUQ_DATABASE_URL`. That is the whole point — two `Funduq` objects
in one process share an event loop and can be made to look like they work by
accident, which is how the first version of this probe overstated what it had
shown.

Usage:

    async with Cluster(nodes=["a", "b"]) as c:
        run = await c.call({"op": "start_run", ...})     # lands wherever
        await c.call({"op": "funduq_start"}, node="b")     # lands on b
        c.kill("a")                                      # SIGKILL, no cleanup
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
FUNDUQ_DIR = REPO_ROOT / "funduq"
NODE_SCRIPT = Path(__file__).resolve().parent / "node.py"

SIGNING_SECRET = "probe-signing-secret"


class NodeError(RuntimeError):
    """A node answered `ok: false`. Carries which node, since 'who said this'
    is half of every finding in a multi-process probe."""


class Cluster:
    """A running cluster of funduq processes, plus the LB that talks to them."""

    def __init__(
        self,
        nodes: list[str],
        *,
        database_url: str | None = None,
        start_nodes: bool = False,
        env: dict[str, str] | None = None,
    ) -> None:
        """`start_nodes` calls `Funduq.start()` on every node at boot, as a real
        deployment would. Off by default: *when* each node reconciles is the
        subject of several probes, not a detail they can leave to chance.
        """
        self.names = list(nodes)
        self.tmpdir = Path(tempfile.mkdtemp(prefix="funduq-probe-"))
        self.database_url = database_url or f"sqlite+aiosqlite:///{self.tmpdir / 'funduq.db'}"
        self.start_nodes = start_nodes
        self.extra_env = env or {}
        self.procs: dict[str, subprocess.Popen] = {}
        self._next = 0

    # ---- Lifecycle

    def _env(self) -> dict[str, str]:
        return {
            **os.environ,
            "FUNDUQ_DATABASE_URL": self.database_url,
            "FUNDUQ_TOKEN_SIGNING_SECRET": SIGNING_SECRET,
            **self.extra_env,
        }

    def migrate(self) -> None:
        """`alembic upgrade head`, once, before any node boots — the same
        separate step a real deployment runs (see funduq/alembic/)."""
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=FUNDUQ_DIR,
            env=self._env(),
            check=True,
            capture_output=True,
        )

    def socket_path(self, name: str) -> Path:
        return self.tmpdir / f"{name}.sock"

    async def __aenter__(self) -> "Cluster":
        self.migrate()
        for name in self.names:
            self.spawn(name)
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        self.shutdown()

    def spawn(self, name: str) -> None:
        """Start one node and wait until its socket is accepting. Waits for the
        node's own READY line rather than sleeping — a probe that sleeps is a
        probe that intermittently measures its own timing."""
        cmd = [sys.executable, str(NODE_SCRIPT), "--name", name, "--socket", str(self.socket_path(name))]
        if self.start_nodes:
            cmd.append("--start")
        proc = subprocess.Popen(
            cmd,
            cwd=FUNDUQ_DIR,
            env=self._env(),
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
        )
        self.procs[name] = proc
        for line in proc.stdout:  # type: ignore[union-attr]
            if line.startswith("READY"):
                return
        raise RuntimeError(f"node {name} exited before becoming ready (rc={proc.poll()})")

    def kill(self, name: str) -> None:
        """SIGKILL, no unwinding — the failure the lease design exists for.
        SIGTERM would let `Funduq.aclose()` run, which is a *graceful* shutdown
        and a different scenario entirely (and one that still leaves its runs
        `running` in the database, by design)."""
        proc = self.procs.pop(name, None)
        if proc is not None:
            proc.send_signal(signal.SIGKILL)
            proc.wait()
        if name in self.names:
            self.names.remove(name)

    def shutdown(self) -> None:
        for proc in self.procs.values():
            proc.terminate()
        for proc in self.procs.values():
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.procs.clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ---- The load balancer

    def pick(self) -> str:
        """Round-robin. No affinity, on purpose — see the module docstring."""
        if not self.names:
            raise RuntimeError("no nodes left in the cluster")
        name = self.names[self._next % len(self.names)]
        self._next += 1
        return name

    async def call(self, request: dict[str, Any], *, node: str | None = None) -> Any:
        """One domain call, through the LB. Returns the node's `result`, and
        raises NodeError if the node reported a failure — a probe should not
        have to check a status field on every line to notice something broke.
        """
        name = node or self.pick()
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path(name)))
        try:
            writer.write((json.dumps(request) + "\n").encode())
            await writer.drain()
            response = json.loads(await reader.readline())
            if not response.get("ok"):
                raise NodeError(f"node {name}: {response.get('error')}")
            return response["result"]
        finally:
            writer.close()
            await writer.wait_closed()

    async def call_on(self, request: dict[str, Any]) -> tuple[str, Any]:
        """Like `call`, but also says which node answered — for probe output
        that has to show the spread rather than assert it happened."""
        name = self.pick()
        return name, await self.call(request, node=name)

    async def subscribe(self, run_id: str, *, node: str, timeout: float = 5.0) -> list[Any]:
        """Drain a run's event stream from one specific node, until the stream
        ends or `timeout` passes. Node-specific by necessity: *which* node can
        answer this is the question, so letting the LB choose would hide it.

        A timeout is an ordinary result here, not an error — a node that is not
        dispatching the run answers with a stream that never ends because it
        never started (see RunBroker.subscribe's `_no_events`).
        """
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path(node)))
        events: list[Any] = []
        try:
            writer.write((json.dumps({"op": "subscribe", "run_id": run_id}) + "\n").encode())
            await writer.drain()
            await reader.readline()  # the {"streaming": true} acknowledgement
            async with asyncio.timeout(timeout):
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    frame = json.loads(line)
                    if frame.get("end"):
                        break
                    events.append(frame["event"])
        except TimeoutError:
            pass
        finally:
            writer.close()
            # Closing a socket whose node was SIGKILLed is expected here, not
            # an error — several probes kill a node mid-stream on purpose.
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        return events
