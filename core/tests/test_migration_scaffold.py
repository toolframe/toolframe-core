import json
import shutil
from pathlib import Path

import pytest
from test_database_providers import provider as provider  # noqa: PLC0414
from toolframe_migration_scaffold import scaffold_migration
from toolframe_migrations import MigrationCoordinator, MigrationError, discover_domains


def domains_root(tmp_path, *names):
    root = tmp_path / "domains"
    for name in names:
        (root / name / "migrations").mkdir(parents=True)
    return root


def complete(draft, table, sql):
    manifest = Path(draft["manifest"])
    content = json.loads(manifest.read_text())
    content["revisions"][-1]["tables"] = [table]
    manifest.write_text(json.dumps(content))
    for path in draft["sources"]:
        Path(path).write_text(sql)


def test_scaffold_upgrade_is_namespaced_and_keeps_applied_bytes(provider, tmp_path):
    root = domains_root(tmp_path, "alpha", "beta")
    first = scaffold_migration(root, "alpha", "create_entries")
    assert first["revision"] == "alpha@0001_create_entries" and first["draft"]
    with pytest.raises(MigrationError, match="owned tables"):
        schemas, legacy = discover_domains(root)
        MigrationCoordinator(provider, schemas, legacy=legacy).check()
    complete(
        first,
        "alpha_entries",
        "CREATE TABLE alpha_entries (id INTEGER PRIMARY KEY, value TEXT); INSERT INTO alpha_entries VALUES (1, 'retained');",
    )
    schemas, legacy = discover_domains(root)
    MigrationCoordinator(provider, schemas, legacy=legacy).apply()
    original = {path: Path(path).read_bytes() for path in first["sources"]}
    second = scaffold_migration(root, "alpha", "add_status")
    assert second["revision"] == "alpha@0002_add_status"
    content = json.loads(Path(second["manifest"]).read_text())
    assert content["revisions"][-1]["depends"] == [first["revision"]]
    complete(
        second,
        "alpha_entries",
        "ALTER TABLE alpha_entries ADD COLUMN status TEXT DEFAULT 'new';",
    )
    schemas, legacy = discover_domains(root)
    assert MigrationCoordinator(provider, schemas, legacy=legacy).apply()[
        "appliedNow"
    ] == [second["revision"]]
    assert provider.query("SELECT value,status FROM alpha_entries").iloc[
        0
    ].tolist() == ["retained", "new"]
    assert all(Path(path).read_bytes() == value for path, value in original.items())
    assert (
        scaffold_migration(root, "beta", "initial")["revision"] == "beta@0001_initial"
    )


def test_scaffold_extends_legacy_without_renumbering_or_editing(provider, tmp_path):
    root = domains_root(tmp_path, "entry_register")
    source = (
        Path(__file__).resolve().parents[2]
        / "domains"
        / "entry_register"
        / "migrations"
    )
    destination = root / "entry_register" / "migrations"
    shutil.copytree(source, destination, dirs_exist_ok=True)
    originals = {path.name: path.read_bytes() for path in destination.glob("*.sql")}
    draft = scaffold_migration(root, "entry_register", "add_status")
    content = json.loads(Path(draft["manifest"]).read_text())
    assert content["revisions"][0]["depends"] == ["legacy@0001"]
    complete(
        draft,
        "entry_register_entries",
        "ALTER TABLE entry_register_entries ADD COLUMN status TEXT;",
    )
    schemas, legacy = discover_domains(root)
    assert MigrationCoordinator(provider, schemas, legacy=legacy).apply()[
        "appliedNow"
    ] == ["legacy@0000", "legacy@0001", draft["revision"]]
    assert all(
        (destination / name).read_bytes() == value for name, value in originals.items()
    )


def test_scaffold_refuses_existing_files_and_lock_and_cleans_its_draft(tmp_path):
    root = domains_root(tmp_path, "alpha")
    directory = root / "alpha" / "migrations"
    (directory / "postgresql").mkdir()
    collision = directory / "postgresql" / "0001_initial.sql"
    collision.write_text("keep my source")
    with pytest.raises(FileExistsError):
        scaffold_migration(root, "alpha", "initial")
    assert collision.read_text() == "keep my source"
    assert not (directory / "schema.json").exists()
    assert not (directory / "sqlite" / "0001_initial.sql").exists()
    assert not (directory / ".scaffold.lock").exists()
    (directory / ".scaffold.lock").touch()
    with pytest.raises(MigrationError) as error:
        scaffold_migration(root, "alpha", "initial")
    assert error.value.code == "scaffold_locked"


@pytest.mark.parametrize(
    "domain,description",
    [
        ("../alpha", "initial"),
        ("alpha", "../escape"),
        ("toolframe", "initial"),
        ("alpha", "x" * 59),
    ],
)
def test_invalid_scaffold_names_do_not_create_files(tmp_path, domain, description):
    root = domains_root(tmp_path, "alpha")
    with pytest.raises(MigrationError) as error:
        scaffold_migration(root, domain, description)
    assert error.value.code == "invalid_scaffold_name"
    assert not list((root / "alpha" / "migrations").iterdir())
