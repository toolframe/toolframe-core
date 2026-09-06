"""Selected-provider lifecycle, safe preflight, and diagnostics contracts."""

import json

import anyio
import pytest
from test_database_providers import provider as provider  # noqa: PLC0414
from toolframe_database import DashboardDatabaseError
from toolframe_database_host import _fingerprint, database_check, database_snapshot
from toolframe_database_service import DATABASE, create_provider
from toolframe_db import Database
from toolframe_idempotency import IdempotencyError, IdempotencyStore
from toolframe_migrations import MigrationError
from toolframe_plugins import configure_plugins


def catalog(tmp_path, *, sql=None):
    app = tmp_path / "app"
    app.mkdir()
    domains = tmp_path / "domains"
    domains.mkdir()
    source = app / "initial.sql"
    source.write_text(
        sql
        or "CREATE TABLE custom_entries (id INTEGER PRIMARY KEY, value TEXT NOT NULL);"
    )
    marker = app / "started"
    (app / "plugins.py").write_text(
        "from pathlib import Path\n"
        "from fastapi_cordis import Plugin\n"
        "from toolframe_database_service import DATABASE\n"
        "from toolframe_migrations import Schema, Revision\n"
        "def setup(ctx):\n"
        "    db = ctx.require(DATABASE)\n"
        "    if db.query('SELECT * FROM custom_entries').empty:\n"
        "        db.execute(\"INSERT INTO custom_entries VALUES (1, 'kept')\")\n"
        f"    Path({str(marker)!r}).write_text('ready')\n"
        "    return db\n"
        "PLUGINS = [Plugin('custom.provider', setup)]\n"
        "SCHEMAS = {'custom.provider': Schema('custom', (Revision('1', "
        f"{{'sqlite': Path({str(source)!r}), 'postgresql': Path({str(source)!r})}}, "
        "('custom_entries',)),), '1')}\n"
    )
    return app, domains, marker


def test_schema_is_ready_before_plugin_and_data_survives_disable_and_restart(
    provider, tmp_path
):
    app, domains, marker = catalog(tmp_path)
    database = Database(provider, ready=False)
    manager = configure_plugins(
        app_dir=app, domains_dir=domains, database=database, artifacts=object()
    )
    with pytest.raises(DashboardDatabaseError) as unavailable:
        database.query("SELECT 1")
    assert unavailable.value.code == "database_not_ready"

    async def exercise():
        async with manager.lifespan():
            assert marker.read_text() == "ready"
            assert manager.migration_state["appliedNow"] == ["custom@1"]
            with manager.lease(DATABASE) as service:
                assert service is database
            await manager.disable("custom.provider")
            assert (
                database.query("SELECT value FROM custom_entries").iloc[0, 0] == "kept"
            )
            await manager.enable("custom.provider")
            assert len(database.query("SELECT * FROM custom_entries")) == 1

    anyio.run(exercise)
    assert provider._closed
    reopened = create_provider(provider.settings)
    try:
        (app / "plugins.py").write_text("PLUGINS = []\n")
        without_plugin = configure_plugins(
            app_dir=app,
            domains_dir=domains,
            database=Database(reopened, ready=False),
            artifacts=object(),
        )

        async def restart():
            async with without_plugin.lifespan():
                assert without_plugin.migration_state["appliedNow"] == []
                assert "custom@1" in without_plugin.migration_state["applied"]
                assert (
                    reopened.query("SELECT value FROM custom_entries").iloc[0, 0]
                    == "kept"
                )

        anyio.run(restart)
    finally:
        reopened.close()


def test_failed_schema_prevents_setup_and_preserves_failed_artifacts(
    provider, tmp_path
):
    app, domains, marker = catalog(
        tmp_path,
        sql="CREATE TABLE custom_entries (id INTEGER PRIMARY KEY, value TEXT); INSERT INTO custom_entries VALUES (1,'a'),(1,'b');",
    )
    manager = configure_plugins(
        app_dir=app,
        domains_dir=domains,
        database=Database(provider, ready=False),
        artifacts=object(),
    )

    async def start():
        async with manager.lifespan():
            pytest.fail("failed migration must prevent readiness")

    with pytest.raises(MigrationError):
        anyio.run(start)
    assert not marker.exists()
    assert provider._closed
    reopened = create_provider(provider.settings)
    try:
        with reopened.connection(read_only=True) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM _toolframe_migration_artifacts"
                ).fetchone()[0]
                == 1
            )
            assert (
                connection.execute(
                    "SELECT status FROM _toolframe_migration_runs"
                ).fetchone()[0]
                == "failed"
            )
        with pytest.raises(DashboardDatabaseError):
            reopened.query("SELECT * FROM custom_entries")
    finally:
        reopened.close()


def test_preflight_reads_and_write_fences_leave_target_unchanged(provider):
    provider.insert("contract_entries", {"value": "original"})
    before = (
        _fingerprint(provider.settings.sqlite_path)
        if provider.dialect == "sqlite"
        else None
    )
    with database_check(provider.settings) as (checked, level, mode):
        if provider.dialect == "sqlite":
            assert level == "schema-trial" and mode == "filesystem-snapshot"
            assert checked.settings.sqlite_path != provider.settings.sqlite_path
            checked.insert("contract_entries", {"value": "only-copy"})
        else:
            assert level == "history-and-plan" and mode == "read-only-target"
        facade = Database(checked, ready=True, read_only=True)
        assert (
            facade.query(
                "SELECT value FROM contract_entries WHERE value='original'"
            ).iloc[0, 0]
            == "original"
        )
        for mutate in (
            lambda: facade.insert("contract_entries", {"value": "forbidden"}),
            lambda: facade.execute("DELETE FROM contract_entries"),
            facade.transaction,
            lambda: facade.run_once("preflight", "key", {}, lambda tx: {}),
        ):
            with pytest.raises(DashboardDatabaseError) as error:
                mutate()
            assert error.value.code == "preflight_write_forbidden"
        with pytest.raises((DashboardDatabaseError, IdempotencyError)):
            IdempotencyStore(provider=facade).run_once(
                "preflight", "key", {}, lambda tx: {}
            )
    if before is not None:
        assert _fingerprint(provider.settings.sqlite_path) == before
    assert list(provider.query("SELECT value FROM contract_entries")["value"]) == [
        "original"
    ]


def test_preview_uses_provider_bounds_and_safe_identity(provider):
    for index in range(3):
        provider.insert("contract_entries", {"value": f"row-{index}"})
    result = database_snapshot(provider, limit=2)
    assert result["database"] == provider.public_info()
    assert result["databasePath"] == provider.public_info()["target"]
    assert [table["name"] for table in result["tables"]] == ["contract_entries"]
    assert result["tables"][0]["truncated"] is True
    assert len(result["tables"][0]["rows"]) == 2
    if provider.settings.postgres_dsn:
        assert provider.settings.postgres_dsn.get_secret_value() not in json.dumps(
            result
        )
