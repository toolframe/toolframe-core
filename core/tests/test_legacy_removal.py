"""Installed legacy files and verified applied archives jointly define history."""

import shutil

import anyio
import pytest
from test_database_providers import provider as provider  # noqa: PLC0414
from test_plugin_migrations import rows
from toolframe_database_service import create_provider
from toolframe_db import Database
from toolframe_migrations import MigrationCoordinator, MigrationError, discover_domains
from toolframe_plugins import configure_plugins


def legacy_layout(root, *, failing=False):
    for number, owner in enumerate(("alpha", "beta", "gamma")):
        directory = root / owner / "migrations"
        companion = directory / "postgresql"
        companion.mkdir(parents=True)
        filename = f"{number:04}_{'bootstrap' if number == 0 else 'initial'}.sql"
        sql = (
            f"CREATE TABLE {owner}_entries (id INTEGER PRIMARY KEY, value TEXT);"
            f"INSERT INTO {owner}_entries VALUES (1,'kept');"
        )
        if failing and owner == "beta":
            sql += "INSERT INTO beta_entries VALUES (1,'duplicate');"
        (directory / filename).write_text(sql)
        (companion / filename).write_text(sql)


def coordinator(provider, root):
    schemas, legacy = discover_domains(root)
    return MigrationCoordinator(provider, schemas, legacy=legacy)


@pytest.mark.parametrize(
    "removed", [("alpha",), ("beta",), ("gamma",), ("alpha", "beta", "gamma")]
)
def test_host_accepts_partial_removal_and_reinstall(provider, tmp_path, removed):
    root = tmp_path / "domains"
    legacy_layout(root)
    sources = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*.sql")
    }
    coordinator(provider, root).apply()
    ledger = rows(provider, "SELECT * FROM _toolframe_migrations ORDER BY version")
    for owner in removed:
        shutil.rmtree(root / owner)

    async def start_host():
        backend = create_provider(provider.settings)
        try:
            manager = configure_plugins(
                app_dir=tmp_path / "app",
                domains_dir=root,
                database=Database(backend, ready=False),
                artifacts=object(),
            )
            async with manager.lifespan():
                assert manager.migration_state["appliedNow"] == []
                assert manager.migration_state["applied"] == [
                    "legacy@0000",
                    "legacy@0001",
                    "legacy@0002",
                ]
                for owner in ("alpha", "beta", "gamma"):
                    assert (
                        backend.query(f"SELECT value FROM {owner}_entries").iloc[0, 0]
                        == "kept"
                    )
                assert (
                    rows(
                        backend, "SELECT * FROM _toolframe_migrations ORDER BY version"
                    )
                    == ledger
                )
        finally:
            backend.close()

    anyio.run(start_host)
    for relative, content in sources.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    anyio.run(start_host)


@pytest.mark.parametrize("archived", [False, True])
def test_missing_unapplied_legacy_revision_cannot_fill_gap(
    provider, tmp_path, archived
):
    root = tmp_path / "domains"
    legacy_layout(root, failing=archived)
    if archived:
        with pytest.raises(MigrationError, match="constraint"):
            coordinator(provider, root).apply()
        assert (
            len(rows(provider, "SELECT sha256 FROM _toolframe_migration_artifacts"))
            == 3
        )
        assert rows(provider, "SELECT ref FROM _toolframe_schema_revisions") == []
    shutil.rmtree(root / "beta")
    with pytest.raises(MigrationError) as error:
        coordinator(provider, root).check()
    assert error.value.code == "legacy_history_gap"


def test_partial_removal_does_not_hide_archive_corruption(provider, tmp_path):
    root = tmp_path / "domains"
    legacy_layout(root)
    coordinator(provider, root).apply()
    shutil.rmtree(root / "beta")
    with provider.connection() as connection:
        connection.execute(
            "UPDATE _toolframe_migration_artifacts SET payload='{}' WHERE sha256 IN "
            "(SELECT artifact_sha FROM _toolframe_schema_revisions WHERE ref='legacy@0001')"
        ).close()
        connection.commit()
    with pytest.raises(MigrationError) as error:
        coordinator(provider, root).check()
    assert error.value.code == "artifact_corrupt"
