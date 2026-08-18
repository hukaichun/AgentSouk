from __future__ import annotations

import logging
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from souk.config import CoreSettings
from souk.core import Souk
from souk.db_schema import EXPECTED_SCHEMA_REVISION



def test_the_expected_revision_matches_the_migrations_actual_head() -> None:
    cfg = Config()
    cfg.set_main_option("script_location", "souk:alembic")
    head = ScriptDirectory.from_config(cfg).get_current_head()

    assert EXPECTED_SCHEMA_REVISION == head, (
        f"souk expects schema revision {EXPECTED_SCHEMA_REVISION} but the migrations' head is "
        f"{head}. Update EXPECTED_SCHEMA_REVISION in souk/db_schema.py — health() compares it "
        "against alembic_version to tell a migrated database from one nobody has migrated yet."
    )


async def test_a_migrated_database_is_ready(souk: Souk) -> None:
    health = await souk.health()

    assert health.database
    assert health.schema_current
    assert health.ready
    assert health.database_error is None


async def test_an_unmigrated_database_is_reachable_but_not_ready(settings: CoreSettings) -> None:
    souk = Souk(settings.model_copy(update={"database_url": "sqlite+aiosqlite:///:memory:"}))
    try:
        health = await souk.health()

        assert health.database
        assert health.schema_revision is None
        assert not health.ready
    finally:
        await souk.aclose()


async def test_an_unreachable_database_says_so_and_leaks_nothing(settings: CoreSettings) -> None:
    souk = Souk(
        settings.model_copy(
            update={"database_url": "postgresql+psycopg://nobody:hunter2@127.0.0.1:1/none"}
        )
    )
    try:
        health = await souk.health(timeout=5)

        assert not health.database
        assert not health.ready
        assert health.database_error == "OperationalError"
        rendered = str(health)
        assert not any(secret in rendered for secret in ("nobody", "hunter2", "127.0.0.1"))
    finally:
        await souk.aclose()


async def test_a_database_at_the_wrong_revision_is_not_ready(settings: CoreSettings, tmp_path) -> None:
    souk = Souk(settings.model_copy(update={"database_url": f"sqlite+aiosqlite:///{tmp_path}/x.db"}))
    try:
        async with souk.engine.begin() as conn:
            await conn.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR)")
            await conn.exec_driver_sql("INSERT INTO alembic_version VALUES ('deadbeef1234')")

        health = await souk.health()

        assert health.database
        assert health.schema_revision == "deadbeef1234"
        assert not health.schema_current
        assert not health.ready
    finally:
        await souk.aclose()


async def test_background_running_reflects_start(settings: CoreSettings) -> None:
    souk = Souk(settings)
    try:
        assert not (await souk.health()).background_running

        await souk.start()
        health = await souk.health()
        assert health.background_running
        assert health.ready
    finally:
        await souk.aclose()


def test_running_migrations_does_not_disable_souks_own_loggers() -> None:
    silenced = [
        name
        for name in ("souk.core", "souk.broker", "souk.handlers", "souk.health", "souk.worker")
        if logging.getLogger(name).disabled
    ]

    assert not silenced, (
        f"running migrations disabled {silenced}. See souk/alembic/env.py — fileConfig needs "
        "disable_existing_loggers=False."
    )
