"""What `Souk.health` is willing to claim.

Two questions get conflated by anything that answers with a bool: whether
the process is alive, and whether it can serve. Only the caller knows which
one it is asking, so this reports facts — reachable, at which migration,
background work running — and leaves the verdict to a probe.

The interesting cases are the failures, since a health check that lies about
them is worse than not having one.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from souk.config import CoreSettings
from souk.core import Souk
from souk.db_schema import EXPECTED_SCHEMA_REVISION

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def test_the_expected_revision_matches_the_migrations_actual_head() -> None:
    """`EXPECTED_SCHEMA_REVISION` is a literal, because souk/alembic/ is not
    shipped inside the package and an installed souk has no migration
    directory to read a head from. This is what stops the literal drifting:
    it fails the moment a migration is added without updating it.
    """
    head = ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_current_head()

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
    """The case a readiness probe exists for: the process is fine, the
    database answers, and it has no schema — which would otherwise be
    discovered as a missing column halfway through someone's request."""
    souk = Souk(settings.model_copy(update={"database_url": "sqlite+aiosqlite:///:memory:"}))
    try:
        health = await souk.health()

        assert health.database
        assert health.schema_revision is None
        assert not health.ready
    finally:
        await souk.aclose()


async def test_an_unreachable_database_says_so_and_leaks_nothing(settings: CoreSettings) -> None:
    """It reported `database=True` when this was first written: reading the
    revision caught DBAPIError to mean "no alembic_version table", and a
    connection failure is an OperationalError, which is one. An unreachable
    database looked merely unmigrated.

    The second assertion matters because a readiness endpoint is normally
    unauthenticated, and a driver's connection error routinely names the
    host and user it tried.
    """
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
    """A deployment that started the new code against a database still on
    the previous migration — reachable, has a schema, wrong one."""
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
    """Reported, not enforced: an embedding caller may deliberately never
    start the sweeps, and that is degraded rather than unservable — so it is
    a fact in the payload and not part of `ready`."""
    souk = Souk(settings)
    try:
        assert not (await souk.health()).background_running

        await souk.start()
        health = await souk.health()
        assert health.background_running
        assert health.ready
    finally:
        await souk.aclose()
