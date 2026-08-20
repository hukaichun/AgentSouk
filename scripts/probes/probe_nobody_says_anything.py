"""Which ways can a provider — or a caller — be left with no answer at all?

Issue #37 in its original form: `claim_work` returned `[]` both for "nothing
queued right now", where waiting is correct, and for "you own none of these",
where waiting is futile. A provider ran 30 minutes on the second one with its
container healthy, its own logs clean and exit code 0, absent from the roster
entirely.

That call is gone — funduq hands work over now, so nothing asks funduq for
anything — which does not retire the question, it moves it. Silence is still
possible; it just happens somewhere else. This asks where.

Each scenario induces one path and reports what actually happens. It exists to
*disprove predictions*: every reasoned conclusion in this repository's recent
history that was checked by running something turned out different.

    cd funduq && uv run python ../scripts/probes/probe_nobody_says_anything.py
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

from funduq.broker import RunBroker
from funduq.config import CoreSettings
from funduq.core import Funduq

from funduq.models import AgentRef
from funduq.schema import agents, providers, run_events, runs, thread_messages, threads
from funduq_provider_sdk import InProcessLink, ProviderIdentity, ProviderRuntime

DB = Path(tempfile.gettempdir()) / "funduq_probe_silence.db"
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


class Agent:
    async def run_stream(self, name: str, run_input):
        ids = {"threadId": run_input.thread_id, "runId": run_input.run_id}
        yield {"type": "RUN_STARTED", **ids}
        yield {"type": "RUN_FINISHED", **ids}


async def register(funduq: Funduq, identity: ProviderIdentity, *names: str):
    signature, timestamp = identity.sign_registration(list(names))
    return await funduq.register_agents(
        identity.public_key, signature, timestamp, [{"name": n} for n in names]
    )


class Findings:
    """Records each path, and fails only on the ones that are supposed to have
    an answer.

    One path is knowingly silent — scenario 3 — so a probe that failed on any
    silence would either be permanently red or get that scenario deleted, and
    both of those lose the record. `must_answer` is the difference between
    "the open edge we decided to live with" and "something that used to be
    answered has stopped being".
    """

    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, bool]] = []

    def record(self, name: str, silent: bool, detail: str, *, must_answer: bool = True) -> None:
        self.rows.append((name, silent, must_answer))
        mark = ("SILENT*" if not must_answer else "SILENT ") if silent else "TOLD   "
        print(f"  {mark} {name}\n           {detail}\n")

    def summarize(self) -> int:
        known = [n for n, s, must in self.rows if s and not must]
        broken = [n for n, s, must in self.rows if s and must]
        if known:
            print(f"* known open edge, not a regression: {', '.join(known)}")
        if broken:
            print(f"REGRESSION: {len(broken)} path(s) that had an answer no longer do:")
            for name in broken:
                print(f"    {name}")
        else:
            print(f"every path that is supposed to answer does ({len(self.rows)} checked)")
        return len(broken)


async def main() -> int:
    migrate()
    findings = Findings()
    funduq = Funduq(CoreSettings(database_url=URL, token_signing_secret="probe"))
    await funduq.start()

    # --- 1. a name this key never registered (a typo, a wrong config)
    print("\n[1] a provider attaches for a name it never registered")
    identity = ProviderIdentity(Ed25519PrivateKey.generate())
    await register(funduq, identity, "translator")
    runtime = ProviderRuntime(identity, Agent())
    runtime.start()
    try:
        await funduq.attach_provider(InProcessLink(funduq, runtime), ["translatr"])
        outcome = "attached, and will now be offered nothing, forever"
        silent = True
    except Exception as exc:
        outcome = f"{type(exc).__name__}: {exc}"
        silent = False
    findings.record(
        "a name never registered",
        silent,
        f"attach_provider {outcome} — the question the old `claim_work` answered "
        "on every call is asked once here, before any run exists",
    )

    # --- 2. a caller's run for an agent nobody is serving
    print("[2] a run is started for an agent no provider is attached to")
    quick = Funduq(
        CoreSettings(database_url=URL, token_signing_secret="probe"),
        broker=RunBroker(unserved_timeout_seconds=0.05),
    )
    await quick.start()
    lonely = ProviderIdentity(Ed25519PrivateKey.generate())
    registered = await register(quick, lonely, "unserved")
    handle = await quick.start_run(registered.agents["unserved"], {"messages": []})
    [_ async for _ in handle.events()]
    async with asyncio.timeout(5):
        while handle.run_id in quick.active_runs():
            await asyncio.sleep(0.01)
    run = await quick.get_run(handle.run_id)
    findings.record(
        "nobody is serving that agent",
        run.status not in ("failed", "cancelled"),
        f"the run ended {run.status}, reason "
        f"{(run.metadata or {}).get('failureReason')!r} — the caller is not left "
        "watching a stream nothing will ever produce for",
    )
    await quick.aclose()

    # --- 3. the database is replaced under an attached provider
    print("[3] funduq's database is replaced while a provider stays attached")
    identity = ProviderIdentity(Ed25519PrivateKey.generate())
    await register(funduq, identity, "steady")
    runtime = ProviderRuntime(identity, Agent())
    runtime.start()
    await funduq.attach_provider(InProcessLink(funduq, runtime), ["steady"])
    async with funduq.session() as session:
        for table in (run_events, thread_messages, runs, threads, agents, providers):
            await session.execute(delete(table))
        await session.commit()

    agent = AgentRef(provider_key=identity.public_key, name="steady")
    still_registered = funduq.broker.serving(agent) is not None
    roster = [a.name for a in await funduq.list_agents()]
    findings.record(
        "the database is replaced under a live attachment",
        still_registered,
        f"broker still routes 'steady' to it: {still_registered}; roster now {roster} "
        "— funduq has no row for this agent and no way to say so, because nothing "
        "asks. The provider finds out by re-attaching, which is scenario 1",
        must_answer=False,
    )

    await runtime.aclose()
    await funduq.aclose()
    print()
    return findings.summarize()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
