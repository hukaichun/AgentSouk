"""Can a running provider survive its database being replaced?

The acceptance check for retiring `agent_id`: an agent is (provider_key,
name), both halves of which the provider already holds.

souk used to mint an id per agent and require a provider to hold it and echo
it back on every claim. That made a provider's whole vocabulary belong to one
particular database. Replace the database and:

- the ids it holds mean nothing to the souk it is talking to, and it cannot
  re-derive them because only souk can mint them;
- re-registering does not fix it either — a fresh database mints *fresh* ids,
  and souk's own in-process worker keeps claiming for the ones it was attached
  with, so `attach_provider` has to be called a second time with the new ones.

That second point is what this probe pins. It is not "an id changed"; it is
that recovering needed a step nobody had a reason to know about. Issue #37 is
the same root: a provider ran 30 minutes looking healthy while claiming for
ids nobody recognised.

An agent is `(provider_key, name)` now. Both halves come from the provider's
own configuration, so nothing it holds can be invalidated by a database it
never saw. Re-registering is the whole repair.

    cd souk && uv run python ../scripts/probes/probe_registration_survives_a_new_database.py
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

from souk.config import CoreSettings
from souk.core import Souk

from souk.identity import registration_signing_payload
from souk.schema import agents, providers, run_events, runs, thread_messages, threads
from souk_provider_sdk import InProcessLink, ProviderIdentity, ProviderRuntime

DB = Path(tempfile.gettempdir()) / "souk_probe_new_database.db"
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


class Provider:
    """Holds only what its own configuration says: the names it serves."""

    async def run_stream(self, agent_name: str, run_input: dict):
        yield {"type": "RUN_STARTED", "threadId": run_input["threadId"], "runId": run_input["runId"]}
        yield {"type": "TEXT_MESSAGE_START", "messageId": "m1", "role": "assistant"}
        yield {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": f"served by {agent_name}"}
        yield {"type": "TEXT_MESSAGE_END", "messageId": "m1"}
        yield {"type": "RUN_FINISHED", "threadId": run_input["threadId"], "runId": run_input["runId"]}


async def main() -> int:
    migrate()
    souk = Souk(CoreSettings(database_url=URL, token_signing_secret="probe"))
    await souk.start()
    identity = ProviderIdentity(Ed25519PrivateKey.generate())
    key, public_key = identity._private_key, identity.public_key

    async def register():
        timestamp = int(time.time())
        return await souk.register_agents(
            public_key,
            key.sign(registration_signing_payload(["translator"], timestamp)).hex(),
            timestamp,
            [{"name": "translator"}],
        )

    first = await register()
    # Attached once, with the names from this provider's own configuration.
    # Through the SDK's runtime, because that is what souk can hand a run to.
    runtime = ProviderRuntime(identity, Provider())
    runtime.start()
    await souk.attach_provider(InProcessLink(souk, runtime), ["translator"])

    handle = await souk.start_run(first.agents["translator"], {"messages": []})
    before = [event async for event in handle.events()]
    print(f"before  : {len(before)} event(s), run reached {(await souk.get_run(handle.run_id)).status}")

    # The database is replaced: a restore from before this provider existed,
    # or souk repointed at a fresh one, while this process keeps running.
    async with souk.session() as session:
        for table in (run_events, thread_messages, runs, threads, agents, providers):
            await session.execute(delete(table))
        await session.commit()
    print("        : database replaced underneath the running provider")

    # The whole repair: register again, with the same names. No re-attach.
    second = await register()
    same_identity = second.agents["translator"] == first.agents["translator"]
    print(f"        : re-registered; same identity as before? {same_identity}")

    handle = await souk.start_run(second.agents["translator"], {"messages": []})
    try:
        async with asyncio.timeout(10):
            after = [event async for event in handle.events()]
    except TimeoutError:
        after = []
    status = (await souk.get_run(handle.run_id)).status
    print(f"after   : {len(after)} event(s), run reached {status}")

    ok = same_identity and status == "completed" and len(after) == len(before)
    print(
        "\nOK   the provider is serving again with no re-attach and no new identifier"
        if ok
        else f"\nBROKEN: same_identity={same_identity} status={status} events={len(after)}"
    )
    await runtime.aclose(cancel_in_flight=True)
    await souk.aclose()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
