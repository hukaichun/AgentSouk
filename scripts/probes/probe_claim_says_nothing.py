"""Which ways can a provider claim forever and be told nothing?

Issue #37: `claim_work` answers `[]` both for "nothing queued right now" —
the normal state of a market stall, wait — and for "you own none of these",
where waiting is futile. A provider ran 30 minutes on the second one with its
container healthy, its own logs clean and exit code 0, absent from the roster
entirely.

A fix for this was written before the structure changed and was deliberately
not carried across (see docs/work-objectives.md, W4): a fix moved through a
refactor is how a fix quietly becomes a workaround. This probe asks the
question again, against what actually got built, and it exists to *disprove
predictions* — every reasoned conclusion in this repository's recent history
that was checked by running something turned out different.

Three trigger paths are predicted. Each scenario induces one and reports what
`claim_work` actually does.

    cd souk && uv run python ../scripts/probes/probe_claim_says_nothing.py
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
from souk.identity import agent_deletion_signing_payload, registration_signing_payload
from souk.schema import agents, providers, run_events, runs, thread_messages, threads

DB = Path(tempfile.gettempdir()) / "souk_probe_nothing_owned.db"
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


class Identity:
    def __init__(self) -> None:
        self.key = Ed25519PrivateKey.generate()
        self.public_key = self.key.public_key().public_bytes_raw().hex()

    async def register(self, souk: Souk, *names: str):
        timestamp = int(time.time())
        return await souk.register_agents(
            self.public_key,
            self.key.sign(registration_signing_payload(list(names), timestamp)).hex(),
            timestamp,
            [{"name": n} for n in names],
        )

    def deletion(self, name: str) -> tuple[str, int]:
        timestamp = int(time.time())
        return self.key.sign(agent_deletion_signing_payload(name, timestamp)).hex(), timestamp


class Findings:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool]] = []

    def record(self, name: str, silent: bool, detail: str) -> None:
        self.rows.append((name, silent))
        print(f"  {'SILENT ' if silent else 'TOLD   '} {name}\n           {detail}\n")

    def summarize(self) -> int:
        silent = [n for n, s in self.rows if s]
        print(f"{len(silent)} of {len(self.rows)} trigger path(s) leave the worker guessing")
        return len(silent)


async def _claim(souk: Souk, token: str, names: list[str]) -> tuple[str, str]:
    """What a worker actually experiences. Returns (outcome, detail)."""
    try:
        claimed = await souk.claim_work(token, names)
        return "returned", f"[] (len {len(claimed)})" if not claimed else f"{len(claimed)} run(s)"
    except Exception as exc:
        return "raised", f"{type(exc).__name__}: {exc}"


async def main() -> int:
    migrate()
    findings = Findings()
    souk = Souk(CoreSettings(database_url=URL, token_signing_secret="probe"))

    # --- 1. the database is replaced under a live connection
    print("\n[1] database replaced while the provider's connection stays open")
    identity = Identity()
    registration = await identity.register(souk, "translator")
    async with souk.session() as session:
        for table in (run_events, thread_messages, runs, threads, agents, providers):
            await session.execute(delete(table))
        await session.commit()

    outcome, detail = await _claim(souk, registration.session_token, ["translator"])
    findings.record(
        "a replaced database",
        outcome == "returned",
        f"claim_work {outcome} {detail} — the provider's token is still valid, its "
        "names are still its own configuration, and nothing it holds went stale; "
        "souk simply has no row for them",
    )

    # --- 2. a name this key never registered
    print("[2] a name the provider never registered (a typo, a wrong config)")
    identity = Identity()
    registration = await identity.register(souk, "translator")

    outcome, detail = await _claim(souk, registration.session_token, ["translatr"])
    findings.record(
        "a name never registered",
        outcome == "returned",
        f"claim_work {outcome} {detail}",
    )
    # The half-right case, which must stay a warning rather than a refusal —
    # and which needs real work queued to demonstrate anything at all. An
    # earlier version of this scenario asked with both names against an empty
    # queue and reported `[]` as if that proved something.
    async with souk.session() as session:
        thread_id = await repo.ensure_thread(session, registration.agents["translator"], None)
        created = await repo.create_run(
            session, thread_id, registration.agents["translator"], "ag-ui", {}
        )
        await session.commit()
    souk.enqueue_run(
        created["run_id"], registration.agents["translator"], thread_id, {}, "ag-ui"
    )

    outcome, detail = await _claim(
        souk, registration.session_token, ["translator", "translatr"]
    )
    findings.record(
        "one good name and one bad, with work queued for the good one",
        False,
        f"claim_work {outcome} {detail} — the typo is filtered out and the real "
        "agent is still served, which is what must not change: killing this "
        "worker over one bad name would be worse than the silence",
    )

    # --- 3. deleted while its provider was offline (new in W3)
    print("[3] the agent was deleted while its provider was offline")
    offline = Souk(
        CoreSettings(
            database_url=URL, token_signing_secret="probe", online_window_seconds=0
        )
    )
    identity = Identity()
    registration = await identity.register(offline, "ephemeral")
    signature, timestamp = identity.deletion("ephemeral")
    await offline.delete_agent(identity.public_key, "ephemeral", signature, timestamp)

    outcome, detail = await _claim(offline, registration.session_token, ["ephemeral"])
    findings.record(
        "deleted while offline, then it came back",
        outcome == "returned",
        f"claim_work {outcome} {detail} — a path that did not exist before deleting "
        "became an explicit act",
    )

    await offline.aclose()
    await souk.aclose()
    print()
    return findings.summarize()


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) == 0 else 1)
