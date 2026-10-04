"""An offline reader proves safety without repairing or migrating state."""

import hashlib
import json
import os
import platform
import sqlite3
import subprocess
from pathlib import Path

import pytest
import tomlkit
from toolframe_sdk import AuthorizationSpec, PluginDefinition, PluginError

from toolframe_core._state_sqlite import SQLiteCoreState
from toolframe_core.config import (
    ConfigFile,
    HostLock,
    PluginRequest,
    atomic_write,
    management_token,
)
from toolframe_core.host import Host
from toolframe_core.maintenance import application_lock, stopped_application

RUN = "a" * 32
SECRET = "credential-value-must-never-appear"


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "application"
    config = ConfigFile(home)
    composition = config.initialize()
    management_token(home)
    lock = HostLock(home)
    lock.acquire()
    lock.close()
    state = SQLiteCoreState(home)
    state.set_meta("host_run", RUN)
    state.set_meta("worker_owners", {})
    state.set_meta("store_bindings", {})
    state.set_meta("last_valid_composition", composition.model_dump())
    state.set_meta("desired_revision", config.revision)
    state.close()
    atomic_write(
        home / "toolframe.lock",
        json.dumps({"format": 1, "desired_revision": config.revision, "plugins": {}}),
    )
    return home


def metadata(home, key, value):
    state = SQLiteCoreState(home)
    state.set_meta(key, value)
    state.close()


def artifact(home, filename, contents):
    checksum = hashlib.sha256(contents).hexdigest()
    relative = f".toolframe/artifacts/{checksum}/{filename}"
    path = home / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    return relative, checksum


def interpreter_metadata(home, environment):
    config = home / f".toolframe/environments/{environment['id']}/pyvenv.cfg"
    atomic_write(
        config,
        f"implementation = {platform.python_implementation()}\nversion_info = {platform.python_version()}\n",
    )
    return config


def install(
    home,
    name="notes",
    *,
    provider_kind=None,
    factory=None,
    secrets=(),
    external=False,
    remote=False,
):
    definition = PluginDefinition(
        id=name,
        factory=factory or "fixture:Plugin",
        kind="provider" if provider_kind else "business",
        provider_kind=provider_kind,
        authorization=AuthorizationSpec(),
        private_secrets=secrets,
        runtime_validation="separate_application" if external else "scratch",
    ).model_dump()
    relative, checksum = artifact(
        home, f"{name.replace('-', '_')}-1.0-py3-none-any.whl", name.encode()
    )
    environment_id = hashlib.sha256((name + "environment").encode()).hexdigest()
    dependency_url = f"file://${{TOOLFRAME_HOME}}/{relative}"
    if remote:
        dependency_url = (
            f"https://username:{SECRET}@packages.invalid/{Path(relative).name}"
        )
        relative_dependency = artifact(home, "other-1.0-py3-none-any.whl", b"other")
        dependency_url = f"https://username:{SECRET}@packages.invalid/{Path(relative_dependency[0]).name}"
        dependency_checksum = relative_dependency[1]
        (home / relative_dependency[0]).unlink()
    else:
        dependency_checksum = checksum
    lock_text = (
        'lock-version = "1.0"\n[[packages]]\n'
        f'name = "{"other" if remote else name}"\nversion = "1.0"\n'
        f'wheels = [{{url = "{dependency_url}", hashes = {{sha256 = "{dependency_checksum}"}}}}]\n'
    )
    candidate = {
        "id": hashlib.sha256((name + "candidate").encode()).hexdigest(),
        "plugin_id": name,
        "version": "1.0",
        "artifact": relative,
        "artifact_hash": checksum,
        "definition": definition,
        "config": {"password": SECRET},
        "source": {"request": f"https://username:{SECRET}@source.invalid/wheel"},
        "environment": {
            "id": environment_id,
            "python": f".toolframe/environments/{environment_id}/bin/python",
            "lock": lock_text,
        },
    }
    interpreter_metadata(home, candidate["environment"])
    config = ConfigFile(home)
    composition, _ = config.read()
    composition.plugins[name] = PluginRequest(source=candidate["source"]["request"])
    atomic_write(config.path, tomlkit.dumps(composition.model_dump()))
    state = SQLiteCoreState(home)
    state.save_candidate(candidate)
    state.select(name, candidate["id"])
    state.runtime(name, {"state": "stopped", "pid": 456789, "host_run": RUN})
    state.set_meta("last_valid_composition", composition.model_dump())
    state.set_meta("desired_revision", config.revision)
    state.close()
    lock = json.loads((home / "toolframe.lock").read_text())
    selected = {
        key: candidate[key]
        for key in (
            "id",
            "version",
            "artifact",
            "artifact_hash",
            "environment",
            "source",
        )
    }
    lock["plugins"][name] = {"current_candidate": selected, "last_candidate": selected}
    lock["desired_revision"] = config.revision
    atomic_write(home / "toolframe.lock", json.dumps(lock))
    return candidate


def business_store(home, owner="notes", *, application_id=None):
    application_id = application_id or ConfigFile(home).read()[0].application.id
    path = home / f"data/plugins/{owner}/data.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as database:
        database.execute(
            "CREATE TABLE _toolframe_identity(owner TEXT, application_id TEXT)"
        )
        database.execute(
            "INSERT INTO _toolframe_identity VALUES(?,?)", (owner, application_id)
        )
    metadata(home, "store_bindings", {owner: "storage-sqlite"})
    return path


def update_candidate(home, candidate):
    state = SQLiteCoreState(home)
    state.save_candidate(candidate)
    state.close()
    lock = json.loads((home / "toolframe.lock").read_text())
    for selected in lock["plugins"][candidate["plugin_id"]].values():
        if selected["id"] == candidate["id"]:
            for key in tuple(selected):
                selected[key] = candidate[key]
    atomic_write(home / "toolframe.lock", json.dumps(lock))


def expect_failure(home, code):
    with pytest.raises(PluginError) as error, stopped_application(home):
        pytest.fail("Unsafe inventory was admitted")
    assert error.value.code == code
    assert SECRET not in str(error.value)
    # Refusal must release the lock, including parser/driver failure paths.
    if (home / ".toolframe/host.lock").is_symlink():
        return
    lock = HostLock(home)
    lock.acquire()
    lock.close()


async def test_real_core_clean_shutdown_is_supported(tmp_path):
    home = tmp_path / "real-core"
    ConfigFile(home).initialize()
    host = await Host.create(home)
    try:
        await host.start()
    finally:
        await host.close()
    before = {
        path.relative_to(home).as_posix(): path.read_bytes()
        for path in home.rglob("*")
        if path.is_file() and path.name != "host.lock"
    }
    with stopped_application(home) as inventory:
        assert inventory.application_id == ConfigFile(home).read()[0].application.id
        assert inventory.status == "STOPPED"
    after = {
        path.relative_to(home).as_posix(): path.read_bytes()
        for path in home.rglob("*")
        if path.is_file() and path.name != "host.lock"
    }
    assert after == before


def test_empty_inventory_is_json_safe_and_does_not_modify_databases(home):
    before = (home / "data/core.sqlite").read_bytes()
    with stopped_application(home) as inventory:
        assert inventory.home == home
        result = inventory.as_dict()
        assert result["format_version"] == 1 and result["status"] == "STOPPED"
        assert "home" not in result
        assert inventory.stores[0].path == "data/core.sqlite"
        assert inventory.retained_keys[0].path == ".toolframe/management.token"
        assert all(item.sha256 is None for item in inventory.files)
        assert {item.path for item in inventory.files} == {
            "toolframe.toml",
            "toolframe.lock",
            "data/core.sqlite",
            ".toolframe/management.token",
        }
        json.dumps(result)
    assert (home / "data/core.sqlite").read_bytes() == before
    assert not (home / "data/core.sqlite-wal").exists()
    assert not (home / "data/core.sqlite-shm").exists()


def test_lock_is_held_for_entire_snapshot_and_released_on_caller_error(home):
    with (
        pytest.raises(RuntimeError, match="snapshot failed"),
        stopped_application(home),
    ):
        competing = HostLock(home)
        with pytest.raises(PluginError) as error:
            competing.acquire()
        assert error.value.code == "host_already_running"
        raise RuntimeError("snapshot failed")
    lock = HostLock(home)
    lock.acquire()
    lock.close()


def test_running_core_lock_is_refused_even_with_blank_pid_file(home):
    owner = HostLock(home)
    owner.acquire()
    owner.file.seek(0)
    owner.file.truncate()
    try:
        with pytest.raises(PluginError) as error, stopped_application(home):
            pytest.fail("Running core admitted")
        assert error.value.code == "host_already_running"
    finally:
        owner.close()


@pytest.mark.parametrize(
    "owners",
    [
        {"999999999": {"name": "notes", "token": f"{RUN}/notes/{'b' * 32}"}},
        [],
        {"invalid": {}},
    ],
)
def test_stale_or_malformed_owners_never_admitted_from_missing_pid(home, owners):
    metadata(home, "worker_owners", owners)
    expect_failure(home, "maintenance_ownership_unproven")


def test_spawn_before_journal_worker_is_refused(home, monkeypatch):
    monkeypatch.setattr(
        "toolframe_core.maintenance.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args,
            0,
            f"321 321 python -m toolframe_sdk.worker --host-token {RUN}/notes/{'b' * 32}\n",
        ),
    )
    expect_failure(home, "maintenance_ownership_unproven")


@pytest.mark.parametrize("output", ["", "unexpected output\n", "1 x python\n"])
def test_process_inventory_uncertainty_is_refused(home, monkeypatch, output):
    monkeypatch.setattr(
        "toolframe_core.maintenance.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, output),
    )
    expect_failure(home, "maintenance_ownership_unproven")


def test_failed_process_probe_is_refused(home, monkeypatch):
    def unavailable(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "ps", stderr=SECRET)

    monkeypatch.setattr("toolframe_core.maintenance.subprocess.run", unavailable)
    expect_failure(home, "maintenance_ownership_unproven")


def test_active_runtime_without_owner_marker_is_uncertain(home):
    install(home)
    state = SQLiteCoreState(home)
    state.runtime("notes", {"state": "active", "pid": 999999999, "host_run": RUN})
    state.close()
    expect_failure(home, "maintenance_ownership_unproven")


def test_layout_artifacts_business_identity_and_secret_redaction(home):
    install(
        home,
        "storage-sqlite",
        provider_kind="storage",
        factory="toolframe_storage_sqlite.plugin:SqliteProvider",
    )
    install(home, secrets=("lookup",))
    business_store(home)
    key = home / ".toolframe/secrets/notes/lookup.key"
    atomic_write(key, SECRET)
    attachment = home / "data/plugins/notes/attachment.txt"
    attachment.write_text("retained attachment")
    # Relocatable state omits worker environments, whose Python symlink is normal.
    environment = home / ".toolframe/environments/regenerable"
    environment.mkdir(parents=True)
    (environment / "python").symlink_to(Path(os.__file__))
    atomic_write(home / ".toolframe/endpoint.json", '{"port": 9876}')
    with stopped_application(home) as inventory:
        text = json.dumps(inventory.as_dict())
        assert SECRET not in text and "packages.invalid" not in text
        assert len(inventory.stores) == 2
        assert any(
            item.owner == "notes" and item.name == "lookup"
            for item in inventory.retained_keys
        )
        paths = {item.path for item in inventory.files}
        assert "data/plugins/notes/attachment.txt" in paths
        assert ".toolframe/endpoint.json" not in paths
        assert not any(path.startswith(".toolframe/environments/") for path in paths)
        assert len(inventory.dependency_wheels) == 2
        assert all(
            item.path and len(item.sha256) == 64 for item in inventory.dependency_wheels
        )
        assert all(
            item.sha256 is None
            for item in inventory.files
            if item.role == "retained_key"
        )


def test_remote_dependency_is_explicit_rehydration_prerequisite(home):
    install(home, remote=True)
    with stopped_application(home) as inventory:
        wheel = inventory.dependency_wheels[0]
        assert wheel.path is None and wheel.filename == "other-1.0-py3-none-any.whl"
        assert len(wheel.sha256) == 64
        text = json.dumps(inventory.as_dict())
        assert SECRET not in text and "packages.invalid" not in text


@pytest.mark.parametrize("external", [False, True])
def test_external_provider_is_refused_even_if_disabled(home, external):
    install(home, "storage-postgres", provider_kind="storage", external=external)
    expect_failure(home, "maintenance_external_state")


def test_external_core_env_is_refused_without_exposing_credentials(home, monkeypatch):
    monkeypatch.setenv("TOOLFRAME_CORE_STATE_BACKEND", "postgresql")
    monkeypatch.setenv(
        "TOOLFRAME_CORE_DATABASE_URL", f"postgresql://user:{SECRET}@host/database"
    )
    monkeypatch.setenv("TOOLFRAME_CORE_APPLICATION_ID", "application")
    monkeypatch.setenv("TOOLFRAME_CORE_NODE_ID", "node")
    expect_failure(home, "maintenance_external_state")


@pytest.mark.parametrize(
    "target",
    [
        "data/core.sqlite",
        ".toolframe/host.lock",
        "data/attachment",
        ".toolframe/secrets",
    ],
)
def test_symlinks_and_escaping_state_are_refused(home, tmp_path, target):
    path = home / target
    if path.exists():
        path.unlink()
    path.symlink_to(tmp_path / "outside")
    expect_failure(home, "maintenance_layout_invalid")


def test_artifact_path_escape_is_refused(home):
    candidate = install(home)
    candidate["artifact"] = "../outside.whl"
    state = SQLiteCoreState(home)
    state.save_candidate(candidate)
    state.close()
    lock = json.loads((home / "toolframe.lock").read_text())
    for selected in lock["plugins"]["notes"].values():
        selected["artifact"] = candidate["artifact"]
    atomic_write(home / "toolframe.lock", json.dumps(lock))
    expect_failure(home, "maintenance_layout_invalid")


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_required_local_artifact_is_verified(home, damage):
    candidate = install(home)
    path = home / candidate["artifact"]
    if damage == "missing":
        path.unlink()
    else:
        path.write_text("changed")
    expect_failure(
        home,
        "maintenance_artifact_missing"
        if damage == "missing"
        else "maintenance_layout_invalid",
    )


def test_business_store_identity_and_missing_key_are_refused(home):
    install(
        home,
        "storage-sqlite",
        provider_kind="storage",
        factory="toolframe_storage_sqlite.plugin:SqliteProvider",
    )
    install(home, secrets=("lookup",))
    store = business_store(home, application_id="another-application")
    expect_failure(home, "maintenance_layout_invalid")
    with sqlite3.connect(store) as database:
        database.execute(
            "UPDATE _toolframe_identity SET application_id=?",
            (ConfigFile(home).read()[0].application.id,),
        )
    expect_failure(home, "maintenance_layout_invalid")


def test_pending_wal_is_refused_without_checkpoint_or_creation(home):
    pending = home / "data/core.sqlite-wal"
    pending.write_bytes(b"uncheckpointed pages")
    expect_failure(home, "maintenance_recovery_required")
    assert pending.read_bytes() == b"uncheckpointed pages"


def test_incompatible_schema_is_not_migrated(home):
    database = home / "data/core.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version=99")
    connection.close()
    before = database.read_bytes()
    expect_failure(home, "maintenance_layout_invalid")
    assert database.read_bytes() == before


def test_interrupted_operation_needs_lifecycle_reconciliation(home):
    state = SQLiteCoreState(home)
    state.new_operation("apply", None, {})
    state.close()
    expect_failure(home, "maintenance_recovery_required")


def test_uninitialized_home_does_not_create_state(tmp_path):
    home = tmp_path / "empty"
    home.mkdir()
    with pytest.raises(PluginError), stopped_application(home):
        pytest.fail("Uninitialized home admitted")
    assert list(home.iterdir()) == []


def test_empty_sqlite_sidecars_are_omitted(home):
    for suffix in ("-wal", "-shm", "-journal"):
        (home / ("data/core.sqlite" + suffix)).touch()
    with stopped_application(home) as inventory:
        assert not any(
            item.path.endswith(("-wal", "-shm", "-journal")) for item in inventory.files
        )


def test_unapplied_composition_cannot_bypass_external_state_detection(home):
    config = ConfigFile(home)
    composition, _ = config.read()
    composition.plugins["storage-postgres"] = PluginRequest(
        source="pypi:toolframe-storage-postgres==0.1.0", enabled=True
    )
    atomic_write(config.path, tomlkit.dumps(composition.model_dump()))
    expect_failure(home, "maintenance_recovery_required")


def test_home_symlink_is_refused(home, tmp_path):
    linked = tmp_path / "linked-home"
    linked.symlink_to(home)
    with pytest.raises(PluginError) as error, stopped_application(linked):
        pytest.fail("Symlink home admitted")
    assert error.value.code == "maintenance_layout_invalid"


@pytest.mark.parametrize(
    "damage",
    [
        "filename_slash",
        "filename_backslash",
        "package_name",
        "package_version",
        "wheel_type",
        "wheel_hash",
        "wheel_identity",
    ],
)
def test_malformed_dependency_metadata_is_refused_without_values(home, damage):
    candidate = install(home)
    lock = tomlkit.parse(candidate["environment"]["lock"]).unwrap()
    package = lock["packages"][0]
    wheel = package["wheels"][0]
    if damage == "filename_slash":
        wheel["url"] = "https://packages.invalid/sub%2Fnotes-1.0-py3-none-any.whl"
    elif damage == "filename_backslash":
        wheel["url"] = "https://packages.invalid/sub%5Cnotes-1.0-py3-none-any.whl"
    elif damage == "package_name":
        package["name"] = f"invalid/{SECRET}"
    elif damage == "package_version":
        package["version"] = f"invalid/{SECRET}"
    elif damage == "wheel_type":
        package["wheels"] = [SECRET]
    elif damage == "wheel_hash":
        wheel["hashes"]["sha256"] = SECRET
    else:
        package["name"] = "other"
    candidate["environment"]["lock"] = tomlkit.dumps(lock)
    update_candidate(home, candidate)
    expect_failure(home, "maintenance_layout_invalid")


def test_unknown_external_provider_kind_and_separate_application_are_refused(home):
    candidate = install(home, "objects", provider_kind="storage")
    candidate["definition"]["provider_kind"] = "object-store"
    update_candidate(home, candidate)
    expect_failure(home, "maintenance_external_state")


def test_retained_previous_and_ui_environment_requirements_are_included(home):
    candidate = install(home)
    previous = dict(candidate)
    previous["id"] = "c" * 64
    previous["artifact"], previous["artifact_hash"] = artifact(
        home, "notes-0.9-py3-none-any.whl", b"retained-old-wheel"
    )
    previous["environment"] = dict(candidate["environment"])
    previous["environment"]["id"] = "d" * 64
    previous["environment"]["python"] = f".toolframe/environments/{'d' * 64}/bin/python"
    previous["ui_environment"] = dict(
        previous["environment"],
        id="e" * 64,
        python=f".toolframe/environments/{'e' * 64}/bin/python",
    )
    interpreter_metadata(home, previous["environment"])
    interpreter_metadata(home, previous["ui_environment"])
    state = SQLiteCoreState(home)
    state.save_candidate(previous)
    state.select("notes", previous["id"])
    state.select("notes", candidate["id"])
    state.save_candidate(candidate)
    state.close()
    lock = json.loads((home / "toolframe.lock").read_text())
    lock["plugins"]["notes"]["previous_candidate"] = {
        key: previous[key] for key in lock["plugins"]["notes"]["current_candidate"]
    }
    atomic_write(home / "toolframe.lock", json.dumps(lock))
    with stopped_application(home) as inventory:
        assert previous["artifact"] in {item.path for item in inventory.files}
        assert {
            (item.candidate_id, item.purpose) for item in inventory.environment_locks
        } == {
            (candidate["id"], "backend"),
            (previous["id"], "backend"),
            (previous["id"], "ui"),
        }
    (home / previous["artifact"]).unlink()
    expect_failure(home, "maintenance_artifact_missing")


def test_protected_environment_lock_is_accessible_but_not_serialized_or_represented(
    home,
):
    candidate = install(home, remote=True)
    with stopped_application(home) as inventory:
        environment = inventory.environment_locks[0]
        assert environment.lock == candidate["environment"]["lock"]
        assert (
            hashlib.sha256(environment.lock.encode()).hexdigest() == environment.sha256
        )
        assert SECRET in environment.lock
        assert SECRET not in repr(environment) and SECRET not in repr(inventory)
        assert "lock" not in inventory.as_dict()["environment_locks"][0]
        assert SECRET not in json.dumps(inventory.as_dict())
        assert environment.python_version == platform.python_version()
        assert environment.python_implementation == platform.python_implementation()
        assert environment.platform_system == platform.system()
        assert environment.platform_machine == platform.machine()


@pytest.mark.parametrize(
    "damage",
    ["missing", "version", "implementation", "symlink", "marker", "requires_python"],
)
def test_source_environment_compatibility_is_required(home, damage, tmp_path):
    candidate = install(home)
    config = (
        home / f".toolframe/environments/{candidate['environment']['id']}/pyvenv.cfg"
    )
    if damage == "missing":
        config.unlink()
    elif damage == "version":
        atomic_write(config, "implementation = CPython\nversion_info = 0.1.0\n")
    elif damage == "implementation":
        atomic_write(
            config,
            f"implementation = Other\nversion_info = {platform.python_version()}\n",
        )
    elif damage == "symlink":
        config.unlink()
        config.symlink_to(tmp_path / "outside")
    else:
        lock = tomlkit.parse(candidate["environment"]["lock"]).unwrap()
        if damage == "marker":
            lock["environments"] = ["sys_platform == 'unavailable-platform'"]
        else:
            lock["requires-python"] = "<1"
        candidate["environment"]["lock"] = tomlkit.dumps(lock)
        update_candidate(home, candidate)
    expect_failure(home, "maintenance_layout_invalid")


def test_application_lock_creates_only_real_lock_state(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setenv("TOOLFRAME_CORE_STATE_BACKEND", "invalid-backend")
    with application_lock(staging) as value:
        assert value is None
        assert {
            path.relative_to(staging).as_posix() for path in staging.rglob("*")
        } == {".toolframe", ".toolframe/host.lock"}
        competing = HostLock(staging)
        with pytest.raises(PluginError) as error:
            competing.acquire()
        assert error.value.code == "host_already_running"
    lock = HostLock(staging)
    lock.acquire()
    lock.close()


def test_application_lock_remains_held_after_home_rename_and_caller_exception(tmp_path):
    staging = tmp_path / "staging"
    final = tmp_path / "destination"
    staging.mkdir()
    with (
        pytest.raises(RuntimeError, match="consumer failed"),
        application_lock(staging),
    ):
        staging.rename(final)
        competing = HostLock(final)
        with pytest.raises(PluginError) as error:
            competing.acquire()
        assert error.value.code == "host_already_running"
        raise RuntimeError("consumer failed")
    lock = HostLock(final)
    lock.acquire()
    lock.close()
    assert not (final / "data").exists()


@pytest.mark.parametrize("unsafe", ["home", "lock_directory", "lock_file", "fifo"])
def test_application_lock_refuses_unsafe_paths(tmp_path, unsafe):
    staging = tmp_path / "staging"
    outside = tmp_path / "outside"
    outside.mkdir()
    if unsafe == "home":
        staging.symlink_to(outside)
    else:
        staging.mkdir()
        if unsafe == "lock_directory":
            (staging / ".toolframe").symlink_to(outside)
        else:
            (staging / ".toolframe").mkdir()
            if unsafe == "fifo":
                os.mkfifo(staging / ".toolframe/host.lock")
            else:
                (staging / ".toolframe/host.lock").symlink_to(outside / "state")
    with pytest.raises(PluginError) as error, application_lock(staging):
        pytest.fail("Unsafe lock path admitted")
    assert error.value.code == "maintenance_layout_invalid"
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("runtime_state", ["unknown", "failed"])
def test_unknown_or_failed_without_pid_is_not_stop_proof(home, runtime_state):
    install(home)
    state = SQLiteCoreState(home)
    state.runtime(
        "notes",
        {
            "state": runtime_state,
            "host_run": RUN,
            "previous_observation": {"state": "stopped"},
        },
    )
    state.close()
    expect_failure(home, "maintenance_ownership_unproven")


def test_exact_empty_disabled_unselected_candidate_can_be_never_started(home):
    install(home)
    state = SQLiteCoreState(home)
    state.connection.execute(
        "UPDATE plugins SET current_candidate=NULL, runtime='{}' WHERE id='notes'"
    )
    state.connection.commit()
    state.close()
    lock = json.loads((home / "toolframe.lock").read_text())
    lock["plugins"]["notes"].pop("current_candidate")
    atomic_write(home / "toolframe.lock", json.dumps(lock))
    with stopped_application(home) as inventory:
        assert inventory.environment_locks


@pytest.mark.parametrize(
    "section", ["application", "bindings", "runtime", "packages", "access"]
)
def test_recorded_semantic_composition_corruption_is_refused(home, section):
    recorded = ConfigFile(home).read()[0].model_dump()
    if section == "application":
        recorded[section]["name"] = "different application name"
    elif section == "bindings":
        recorded[section]["notes/storage"] = "external-storage"
    elif section == "runtime":
        recorded[section]["drain_timeout"] += 1
    elif section == "packages":
        recorded[section]["offline"] = True
    else:
        recorded[section]["mode"] = "protected"
    metadata(home, "last_valid_composition", recorded)
    expect_failure(home, "maintenance_layout_invalid")


def test_recorded_composition_receives_backward_compatible_model_defaults(home):
    recorded = ConfigFile(home).read()[0].model_dump()
    recorded["runtime"].pop("max_task_timeout")
    metadata(home, "last_valid_composition", recorded)
    with stopped_application(home) as inventory:
        assert inventory.status == "STOPPED"


async def test_real_core_package_overrides_require_matching_operator_environment(
    tmp_path, monkeypatch
):
    home = tmp_path / "overridden-core"
    wheelhouse = str(tmp_path / "wheelhouse")
    monkeypatch.setenv("TOOLFRAME_WHEELHOUSE", wheelhouse)
    monkeypatch.setenv("TOOLFRAME_OFFLINE", "1")
    ConfigFile(home).initialize()
    host = await Host.create(home)
    try:
        await host.start()
    finally:
        await host.close()
    with stopped_application(home) as inventory:
        assert inventory.status == "STOPPED"
    monkeypatch.delenv("TOOLFRAME_WHEELHOUSE")
    monkeypatch.delenv("TOOLFRAME_OFFLINE")
    expect_failure(home, "maintenance_layout_invalid")


@pytest.mark.parametrize("selection_field", ["source", "version", "ui_environment"])
def test_candidate_lock_source_version_and_present_ui_corruption_is_refused(
    home, selection_field
):
    install(home)
    lock = json.loads((home / "toolframe.lock").read_text())
    for selected in lock["plugins"]["notes"].values():
        selected[selection_field] = (
            {"request": SECRET} if selection_field == "source" else SECRET
        )
    atomic_write(home / "toolframe.lock", json.dumps(lock))
    expect_failure(home, "maintenance_layout_invalid")


def test_current_composition_plugin_without_candidate_is_refused(home):
    install(home)
    state = SQLiteCoreState(home)
    state.connection.execute(
        "UPDATE plugins SET current_candidate=NULL, last_candidate=NULL, previous_candidate=NULL WHERE id='notes'"
    )
    state.connection.commit()
    state.close()
    lock = json.loads((home / "toolframe.lock").read_text())
    lock["plugins"]["notes"] = {}
    atomic_write(home / "toolframe.lock", json.dumps(lock))
    expect_failure(home, "maintenance_layout_invalid")


def test_ghost_retained_store_owner_is_refused_despite_matching_database_identity(home):
    install(
        home,
        "storage-sqlite",
        provider_kind="storage",
        factory="toolframe_storage_sqlite.plugin:SqliteProvider",
    )
    business_store(home, owner="ghost")
    expect_failure(home, "maintenance_external_state")


@pytest.mark.parametrize("recorded_run", ["missing", None, "invalid"])
def test_stopped_observation_requires_valid_recorded_host_run(home, recorded_run):
    install(home)
    observed = {"state": "stopped"}
    if recorded_run != "missing":
        observed["host_run"] = recorded_run
    state = SQLiteCoreState(home)
    state.runtime("notes", observed)
    state.close()
    expect_failure(home, "maintenance_ownership_unproven")


def test_binary_wheels_must_match_source_platform_without_environment_markers(home):
    candidate = install(home)
    lock = tomlkit.parse(candidate["environment"]["lock"]).unwrap()
    relative, checksum = artifact(
        home, "notes-1.0-cp314-cp314-win_amd64.whl", b"wrong-platform-wheel"
    )
    lock["packages"][0]["wheels"] = [
        {
            "url": f"file://${{TOOLFRAME_HOME}}/{relative}",
            "hashes": {"sha256": checksum},
        }
    ]
    candidate["environment"]["lock"] = tomlkit.dumps(lock)
    update_candidate(home, candidate)
    expect_failure(home, "maintenance_layout_invalid")


def test_empty_observation_with_previous_selection_is_not_never_started(home):
    candidate = install(home)
    state = SQLiteCoreState(home)
    state.connection.execute(
        "UPDATE plugins SET current_candidate=NULL, previous_candidate=?, runtime='{}' WHERE id='notes'",
        (candidate["id"],),
    )
    state.connection.commit()
    state.close()
    lock = json.loads((home / "toolframe.lock").read_text())
    selected = lock["plugins"]["notes"].pop("current_candidate")
    lock["plugins"]["notes"]["previous_candidate"] = selected
    atomic_write(home / "toolframe.lock", json.dumps(lock))
    expect_failure(home, "maintenance_ownership_unproven")


def test_consistent_but_unsafe_candidate_identifier_is_refused(home):
    candidate = install(home)
    old = candidate["id"]
    candidate["id"] = "../../unsafe-candidate"
    state = SQLiteCoreState(home)
    state.connection.execute(
        "UPDATE candidates SET id=?, data=? WHERE id=?",
        (candidate["id"], json.dumps(candidate), old),
    )
    state.connection.execute(
        "UPDATE plugins SET current_candidate=?, last_candidate=? WHERE id='notes'",
        (candidate["id"], candidate["id"]),
    )
    state.connection.commit()
    state.close()
    lock = json.loads((home / "toolframe.lock").read_text())
    for selected in lock["plugins"]["notes"].values():
        selected["id"] = candidate["id"]
    atomic_write(home / "toolframe.lock", json.dumps(lock))
    expect_failure(home, "maintenance_layout_invalid")
