from __future__ import annotations

import logging
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from funduq.config import CoreSettings
from funduq.core import Funduq
from funduq.db_schema import EXPECTED_SCHEMA_REVISION



def test_the_expected_revision_matches_the_migrations_actual_head() -> None:
    cfg = Config()
    cfg.set_main_option("script_location", "funduq:alembic")
    head = ScriptDirectory.from_config(cfg).get_current_head()

    assert EXPECTED_SCHEMA_REVISION == head, (
        f"funduq expects schema revision {EXPECTED_SCHEMA_REVISION} but the migrations' head is "
        f"{head}. Update EXPECTED_SCHEMA_REVISION in funduq/db_schema.py — health() compares it "
        "against alembic_version to tell a migrated database from one nobody has migrated yet."
    )


async def test_a_migrated_database_is_ready(funduq: Funduq) -> None:
    health = await funduq.health()

    assert health.database
    assert health.schema_current
    assert health.ready
    assert health.database_error is None


async def test_an_unmigrated_database_is_reachable_but_not_ready(settings: CoreSettings) -> None:
    funduq = Funduq(settings.model_copy(update={"database_url": "sqlite+aiosqlite:///:memory:"}))
    try:
        health = await funduq.health()

        assert health.database
        assert health.schema_revision is None
        assert not health.ready
    finally:
        await funduq.aclose()


async def test_an_unreachable_database_says_so_and_leaks_nothing(settings: CoreSettings) -> None:
    funduq = Funduq(
        settings.model_copy(
            update={"database_url": "postgresql+psycopg://nobody:hunter2@127.0.0.1:1/none"}
        )
    )
    try:
        health = await funduq.health(timeout=5)

        assert not health.database
        assert not health.ready
        assert health.database_error == "OperationalError"
        rendered = str(health)
        assert not any(secret in rendered for secret in ("nobody", "hunter2", "127.0.0.1"))
    finally:
        await funduq.aclose()


async def test_a_database_at_the_wrong_revision_is_not_ready(settings: CoreSettings, tmp_path) -> None:
    funduq = Funduq(settings.model_copy(update={"database_url": f"sqlite+aiosqlite:///{tmp_path}/x.db"}))
    try:
        async with funduq.engine.begin() as conn:
            await conn.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR)")
            await conn.exec_driver_sql("INSERT INTO alembic_version VALUES ('deadbeef1234')")

        health = await funduq.health()

        assert health.database
        assert health.schema_revision == "deadbeef1234"
        assert not health.schema_current
        assert not health.ready
    finally:
        await funduq.aclose()


async def test_background_running_reflects_start(settings: CoreSettings) -> None:
    funduq = Funduq(settings)
    try:
        assert not (await funduq.health()).background_running

        await funduq.start()
        health = await funduq.health()
        assert health.background_running
        assert health.ready
    finally:
        await funduq.aclose()


def test_running_migrations_does_not_disable_funduqs_own_loggers() -> None:
    silenced = [
        name
        for name in ("funduq.core", "funduq.broker", "funduq.handlers", "funduq.health", "funduq.worker")
        if logging.getLogger(name).disabled
    ]

    assert not silenced, (
        f"running migrations disabled {silenced}. See funduq/alembic/env.py — fileConfig needs "
        "disable_existing_loggers=False."
    )
