import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import toolframe_migrations as migrations
from test_database_providers import (
    provider as provider,  # noqa: PLC0414 - pytest fixture re-export.
)
from toolframe_migrations import MigrationCoordinator, MigrationError, Revision, Schema


def schema(
    tmp_path,
    *,
    owner="alpha",
    revision="1",
    sql=None,
    depends=(),
    tables=None,
    reads=(),
):
    source = tmp_path / f"{owner}_{revision}.sql"
    if sql is None:
        sql = f"CREATE TABLE {owner}_entries (id INTEGER PRIMARY KEY, value TEXT NOT NULL); INSERT INTO {owner}_entries (id,value) VALUES (1,'first');"
    source.write_text(sql)
    declaration = Revision(
        revision,
        {"sqlite": source, "postgresql": source},
        tables or (f"{owner}_entries",),
        depends,
        reads,
    )
    return Schema(owner, (declaration,), revision)


def rows(provider, sql, params=()):
    with provider.connection(read_only=True) as connection:
        return migrations._rows(connection, sql, params)


def test_first_repeat_and_durable_artifacts(provider, tmp_path):
    alpha = schema(tmp_path)
    coordinator = MigrationCoordinator(provider, [alpha])
    assert coordinator.check()["pending"] == ["alpha@1"]
    assert coordinator.apply()["appliedNow"] == ["alpha@1"]
    assert coordinator.apply()["appliedNow"] == []
    assert provider.query("SELECT value FROM alpha_entries").iloc[0, 0] == "first"
    artifact = rows(
        provider, "SELECT sha256,payload FROM _toolframe_migration_artifacts"
    )[0]
    assert (
        hashlib.sha256(artifact["payload"].encode()).hexdigest() == artifact["sha256"]
    )
    metadata = json.loads(artifact["payload"])
    assert metadata["owner"] == "alpha"
    assert metadata["depends"] == []
    assert metadata["tables"] == ["alpha_entries"]
    assert set(metadata["sql"]) == {"sqlite", "postgresql"}
    assert all(
        value["base64"] and value["sha256"] for value in metadata["sql"].values()
    )


def test_upgrade_and_dependency_order(provider, tmp_path):
    alpha = schema(tmp_path)
    MigrationCoordinator(provider, [alpha]).apply()
    beta = schema(
        tmp_path,
        owner="beta",
        sql="CREATE TABLE beta_entries (id INTEGER PRIMARY KEY, value TEXT); INSERT INTO beta_entries SELECT id,value FROM alpha_entries;",
        depends=("alpha@1",),
        reads=("alpha_entries",),
    )
    upgrade = schema(
        tmp_path,
        revision="2",
        sql="ALTER TABLE alpha_entries ADD COLUMN version INTEGER NOT NULL DEFAULT 1;",
        depends=("alpha@1", "beta@1"),
    )
    upgraded = Schema("alpha", (*alpha.revisions, *upgrade.revisions), "2")
    result = MigrationCoordinator(provider, [upgraded, beta]).apply()
    assert result["appliedNow"] == ["beta@1", "alpha@2"]
    assert provider.query("SELECT version FROM alpha_entries").iloc[0, 0] == 1
    assert provider.query("SELECT value FROM beta_entries").iloc[0, 0] == "first"


def test_artifacts_commit_before_sql_and_survive_failure(
    provider, tmp_path, monkeypatch
):
    alpha = schema(
        tmp_path,
        sql="CREATE TABLE alpha_entries (id INTEGER PRIMARY KEY, value TEXT NOT NULL); INSERT INTO alpha_entries (id,value) VALUES (1,NULL);",
    )
    original = migrations.execute_script
    observed = []

    def verify_then_execute(connection, artifact, dialect, owners):
        observed.extend(
            rows(provider, "SELECT payload FROM _toolframe_migration_artifacts")
        )
        assert rows(provider, "SELECT status FROM _toolframe_migration_runs") == [
            {"status": "prepared"}
        ]
        return original(connection, artifact, dialect, owners)

    monkeypatch.setattr(migrations, "execute_script", verify_then_execute)
    with pytest.raises(MigrationError) as raised:
        MigrationCoordinator(provider, [alpha]).apply()
    assert raised.value.code == "migration_sql_failed"
    assert len(observed) == 1
    assert rows(provider, "SELECT status FROM _toolframe_migration_runs") == [
        {"status": "failed"}
    ]
    assert rows(provider, "SELECT ref FROM _toolframe_schema_revisions") == []
    with provider.connection(read_only=True) as connection:
        assert "alpha_entries" not in migrations._table_names(
            connection, provider.dialect
        )
    monkeypatch.setattr(migrations, "execute_script", original)
    corrected = schema(tmp_path)
    assert MigrationCoordinator(provider, [corrected]).apply()["appliedNow"] == [
        "alpha@1"
    ]
    assert len(rows(provider, "SELECT sha256 FROM _toolframe_migration_artifacts")) == 2


def test_removed_plugin_keeps_data_and_verifiable_history(provider, tmp_path):
    alpha = schema(tmp_path)
    MigrationCoordinator(provider, [alpha]).apply()
    source = next(iter(alpha.revisions[0].sql.values()))
    original_bytes = source.read_bytes()
    source.unlink()
    absent = MigrationCoordinator(provider, [])
    assert absent.check()["applied"] == ["alpha@1"]
    assert absent.apply()["appliedNow"] == []
    assert provider.query("SELECT value FROM alpha_entries").iloc[0, 0] == "first"
    source.write_bytes(original_bytes)
    assert MigrationCoordinator(provider, [alpha]).apply()["appliedNow"] == []


def test_archived_pending_sql_never_runs_for_removed_plugin(provider, tmp_path):
    alpha = schema(
        tmp_path,
        sql="CREATE TABLE alpha_entries (id INTEGER PRIMARY KEY); INSERT INTO alpha_entries VALUES (1); INSERT INTO alpha_entries VALUES (1);",
    )
    with pytest.raises(MigrationError):
        MigrationCoordinator(provider, [alpha]).apply()
    assert MigrationCoordinator(provider, []).apply()["appliedNow"] == []
    with provider.connection(read_only=True) as connection:
        assert "alpha_entries" not in migrations._table_names(
            connection, provider.dialect
        )
    assert len(rows(provider, "SELECT sha256 FROM _toolframe_migration_artifacts")) == 1


def test_reinstall_rejects_downgrade_unless_compatibility_is_explicit(
    provider, tmp_path
):
    first = schema(tmp_path)
    second = schema(
        tmp_path,
        revision="2",
        sql="ALTER TABLE alpha_entries ADD COLUMN version INTEGER DEFAULT 1;",
        depends=("alpha@1",),
    )
    current = Schema("alpha", (*first.revisions, *second.revisions), "2")
    MigrationCoordinator(provider, [current]).apply()
    MigrationCoordinator(provider, []).apply()
    with pytest.raises(MigrationError) as raised:
        MigrationCoordinator(provider, [first]).apply()
    assert raised.value.code == "incompatible_schema"
    compatible = Schema("alpha", first.revisions, "1", ("2",))
    assert MigrationCoordinator(provider, [compatible]).apply()["appliedNow"] == []
    assert provider.query("SELECT version FROM alpha_entries").iloc[0, 0] == 1


def test_applied_checksum_change_is_rejected(provider, tmp_path):
    original = schema(tmp_path)
    MigrationCoordinator(provider, [original]).apply()
    changed = schema(
        tmp_path,
        sql="CREATE TABLE alpha_entries (id INTEGER PRIMARY KEY, value TEXT); -- changed",
    )
    with pytest.raises(MigrationError) as raised:
        MigrationCoordinator(provider, [changed]).apply()
    assert raised.value.code == "checksum_mismatch"
    assert provider.query("SELECT value FROM alpha_entries").iloc[0, 0] == "first"


def test_archive_corruption_is_detected_after_uninstall(provider, tmp_path):
    MigrationCoordinator(provider, [schema(tmp_path)]).apply()
    with provider.connection() as connection:
        connection.execute(
            "UPDATE _toolframe_migration_artifacts SET payload='{}'"
        ).close()
        connection.commit()
    with pytest.raises(MigrationError) as raised:
        MigrationCoordinator(provider, []).check()
    assert raised.value.code == "artifact_corrupt"


@pytest.mark.parametrize(
    "case", ["missing", "cycle", "read_without_dependency", "cross_owner_write"]
)
def test_invalid_dependency_and_ownership_plans_fail(provider, tmp_path, case):
    if case == "missing":
        definitions = [schema(tmp_path, depends=("missing@1",))]
        expected = "missing_dependency"
    elif case == "cycle":
        definitions = [
            schema(tmp_path, depends=("beta@1",)),
            schema(tmp_path, owner="beta", depends=("alpha@1",)),
        ]
        expected = "dependency_cycle"
    elif case == "read_without_dependency":
        definitions = [
            schema(tmp_path),
            schema(
                tmp_path,
                owner="beta",
                sql="CREATE TABLE beta_entries (id INTEGER PRIMARY KEY); INSERT INTO beta_entries SELECT id FROM alpha_entries;",
                reads=("alpha_entries",),
            ),
        ]
        expected = "missing_read_dependency"
    else:
        definitions = [
            schema(tmp_path),
            schema(
                tmp_path,
                owner="beta",
                sql="UPDATE alpha_entries SET value='denied';",
                depends=("alpha@1",),
                reads=("alpha_entries",),
            ),
        ]
        expected = "table_write_denied"
    with pytest.raises(MigrationError) as raised:
        MigrationCoordinator(provider, definitions).apply()
    assert raised.value.code == expected


def test_concurrent_startups_apply_once(provider, tmp_path):
    alpha = schema(tmp_path)
    barrier = threading.Barrier(2)

    def run(_):
        barrier.wait(timeout=10)
        return MigrationCoordinator(provider, [alpha]).apply()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, range(2)))
    assert sorted(len(result["appliedNow"]) for result in results) == [0, 1]
    assert provider.query("SELECT value FROM alpha_entries")["value"].tolist() == [
        "first"
    ]


def test_history_ownership_remains_reserved_after_uninstall(provider, tmp_path):
    alpha = schema(tmp_path)
    MigrationCoordinator(provider, [alpha]).apply()
    MigrationCoordinator(provider, []).apply()
    assert rows(provider, "SELECT table_name,owner FROM _toolframe_schema_owners") == [
        {"table_name": "alpha_entries", "owner": "alpha"}
    ]
    with pytest.raises(MigrationError) as raised:
        MigrationCoordinator(
            provider, [schema(tmp_path, owner="beta", tables=("alpha_entries",))]
        )
    assert raised.value.code == "invalid_ownership"


def test_preserved_legacy_files_work_on_both_providers(provider):
    root = Path(__file__).resolve().parents[2]
    paths = sorted((root / "domains/entry_register/migrations").glob("*.sql"))
    originals = {path: path.read_bytes() for path in paths}
    schemas, legacy = migrations.discover_domains(root / "domains")
    first = MigrationCoordinator(provider, schemas, legacy=legacy).apply()
    assert first["appliedNow"] == ["legacy@0000", "legacy@0001"]
    identifier = provider.insert(
        "entry_register_entries", {"label": "kept", "value": 1.0}
    )
    before = rows(provider, "SELECT * FROM _toolframe_migrations ORDER BY version")
    assert (
        MigrationCoordinator(provider, schemas, legacy=legacy).apply()["appliedNow"]
        == []
    )
    assert before == rows(
        provider, "SELECT * FROM _toolframe_migrations ORDER BY version"
    )
    assert (
        provider.query("SELECT id FROM entry_register_entries").iloc[0, 0] == identifier
    )
    assert all(path.read_bytes() == data for path, data in originals.items())


def test_existing_sqlite_legacy_ledger_is_adopted_without_rewriting(tmp_path):
    from toolframe_database_config import DatabaseSettings
    from toolframe_database_service import create_provider

    db = create_provider(DatabaseSettings(sqlite_path=tmp_path / "legacy.db"))
    root = Path(__file__).resolve().parents[2]
    paths = sorted((root / "domains/entry_register/migrations").glob("*.sql"))
    try:
        with db.connection() as connection:
            connection.execute(migrations._LEDGER_SCHEMA).close()
            for index, path in enumerate(paths):
                source = path.read_bytes()
                connection.execute(source.decode()).close()
                connection.execute(
                    "INSERT INTO _toolframe_migrations(version,filename,sha256,applied_at) VALUES (:v,:name,:hash,:time)",
                    {
                        "v": index,
                        "name": path.name,
                        "hash": hashlib.sha256(source).hexdigest(),
                        "time": "original-time",
                    },
                ).close()
            connection.commit()
        identifier = db.insert(
            "entry_register_entries", {"label": "old data", "value": 2.0}
        )
        before = rows(db, "SELECT * FROM _toolframe_migrations ORDER BY version")
        schemas, legacy = migrations.discover_domains(root / "domains")
        report = MigrationCoordinator(db, schemas, legacy=legacy).apply()
        assert report["appliedNow"] == []
        assert (
            rows(db, "SELECT * FROM _toolframe_migrations ORDER BY version") == before
        )
        assert (
            db.query("SELECT id FROM entry_register_entries").iloc[0, 0] == identifier
        )
        assert len(rows(db, "SELECT ref FROM _toolframe_schema_revisions")) == 2
    finally:
        db.close()


def test_uninstalled_pending_archive_does_not_satisfy_dependency(provider, tmp_path):
    alpha = schema(
        tmp_path,
        sql="CREATE TABLE alpha_entries (id INTEGER PRIMARY KEY); INSERT INTO alpha_entries VALUES (1); INSERT INTO alpha_entries VALUES (1);",
    )
    with pytest.raises(MigrationError):
        MigrationCoordinator(provider, [alpha]).apply()
    beta = schema(tmp_path, owner="beta", depends=("alpha@1",))
    with pytest.raises(MigrationError) as raised:
        MigrationCoordinator(provider, [beta]).apply()
    assert raised.value.code == "missing_dependency"


def test_dependency_cannot_silently_upgrade_another_plugins_schema(provider, tmp_path):
    first = schema(tmp_path)
    second = schema(
        tmp_path,
        revision="2",
        sql="ALTER TABLE alpha_entries ADD COLUMN version INTEGER;",
        depends=("alpha@1",),
    )
    older_code = Schema("alpha", (*first.revisions, *second.revisions), "1")
    beta = schema(tmp_path, owner="beta", depends=("alpha@2",))
    with pytest.raises(MigrationError) as raised:
        MigrationCoordinator(provider, [older_code, beta]).apply()
    assert raised.value.code == "incompatible_schema"


def test_missing_ownership_and_mismatched_artifact_are_detected(provider, tmp_path):
    first = schema(tmp_path)
    second = schema(
        tmp_path,
        revision="2",
        sql="ALTER TABLE alpha_entries ADD COLUMN version INTEGER;",
        depends=("alpha@1",),
    )
    installed = Schema("alpha", (*first.revisions, *second.revisions), "1")
    MigrationCoordinator(provider, [installed]).apply()
    with provider.connection() as connection:
        connection.execute("DELETE FROM _toolframe_schema_owners").close()
        connection.commit()
    with pytest.raises(MigrationError) as raised:
        MigrationCoordinator(provider, []).check()
    assert raised.value.code == "ownership_history_missing"
    artifacts = rows(
        provider, "SELECT sha256,payload FROM _toolframe_migration_artifacts"
    )
    wrong = next(
        item["sha256"]
        for item in artifacts
        if json.loads(item["payload"])["ref"] == "alpha@2"
    )
    with provider.connection() as connection:
        connection.execute(
            "UPDATE _toolframe_schema_revisions SET artifact_sha=:hash", {"hash": wrong}
        ).close()
        connection.commit()
    with pytest.raises(MigrationError) as raised:
        MigrationCoordinator(provider, []).check()
    assert raised.value.code == "missing_artifact"


def test_source_change_after_archival_is_rejected(provider, tmp_path, monkeypatch):
    alpha = schema(tmp_path)
    original = migrations._initialize_history

    def mutate_after_plan(connection, dialect):
        original(connection, dialect)
        next(iter(alpha.revisions[0].sql.values())).write_text(
            "-- replaced during startup"
        )

    monkeypatch.setattr(migrations, "_initialize_history", mutate_after_plan)
    with pytest.raises(MigrationError) as raised:
        MigrationCoordinator(provider, [alpha]).apply()
    assert raised.value.code == "source_changed"
    assert rows(provider, "SELECT ref FROM _toolframe_schema_revisions") == []
    assert len(rows(provider, "SELECT sha256 FROM _toolframe_migration_artifacts")) == 1


def test_switching_target_never_copies_or_removes_old_rows(provider, tmp_path):
    from toolframe_database_config import DatabaseSettings
    from toolframe_database_service import create_provider

    alpha = schema(
        tmp_path,
        sql="CREATE TABLE alpha_entries (id INTEGER PRIMARY KEY, value TEXT NOT NULL);",
    )
    previous = create_provider(DatabaseSettings(sqlite_path=tmp_path / "previous.db"))
    try:
        MigrationCoordinator(previous, [alpha]).apply()
        previous.insert("alpha_entries", {"id": 1, "value": "old target only"})
        MigrationCoordinator(provider, [alpha]).apply()
        assert provider.query("SELECT * FROM alpha_entries").empty
        provider.insert("alpha_entries", {"id": 2, "value": "new target only"})
        assert previous.query("SELECT value FROM alpha_entries")["value"].tolist() == [
            "old target only"
        ]
        assert provider.query("SELECT value FROM alpha_entries")["value"].tolist() == [
            "new target only"
        ]
    finally:
        previous.close()


def test_runtime_read_limits_do_not_truncate_migration_archives(provider, tmp_path):
    from toolframe_database_service import create_provider

    limited = create_provider(
        provider.settings.model_copy(update={"max_bytes": 128, "max_rows": 1})
    )
    try:
        alpha = schema(tmp_path)
        assert MigrationCoordinator(limited, [alpha]).apply()["appliedNow"] == [
            "alpha@1"
        ]
        assert MigrationCoordinator(limited, []).check()["applied"] == ["alpha@1"]
    finally:
        limited.close()


def test_create_and_drop_owned_index(provider, tmp_path):
    alpha = schema(
        tmp_path,
        sql="CREATE TABLE alpha_entries (id INTEGER PRIMARY KEY, value TEXT); CREATE INDEX alpha_value ON alpha_entries(value);",
    )
    MigrationCoordinator(provider, [alpha]).apply()
    drop = schema(
        tmp_path, revision="2", sql="DROP INDEX alpha_value;", depends=("alpha@1",)
    )
    upgraded = Schema("alpha", (*alpha.revisions, *drop.revisions), "2")
    assert MigrationCoordinator(provider, [upgraded]).apply()["appliedNow"] == [
        "alpha@2"
    ]


def test_long_revision_history_does_not_exhaust_python_recursion():
    artifacts = {
        f"legacy@{index:04}": migrations.Artifact(
            {
                "ref": f"legacy@{index:04}",
                "depends": [f"legacy@{index - 1:04}"] if index else [],
            }
        )
        for index in range(10000)
    }
    ordered = migrations._order(["legacy@9999"], artifacts)
    assert len(ordered) == 10000
    assert ordered[0].ref == "legacy@0000" and ordered[-1].ref == "legacy@9999"
