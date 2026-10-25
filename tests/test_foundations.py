from pathlib import Path

import pytest
from dependencies import wheelhouse
from toolframe_sdk import PluginError

from toolframe_core.config import ConfigFile, HostLock, PackageSettings, PluginRequest
from toolframe_core.packages import Packages, snapshot
from toolframe_core.state import SQLiteCoreState as CoreState


def test_packages_use_selected_uv_even_when_renamed(tmp_path, monkeypatch):
    selected = tmp_path / "selected uv"
    selected.write_text("#!/bin/sh\nexit 0\n")
    selected.chmod(0o755)
    monkeypatch.setenv("TOOLFRAME_UV", str(selected))
    monkeypatch.setenv("PATH", "")
    package = Packages(tmp_path / "home", PackageSettings())
    assert package.uv == str(selected.resolve())


def test_packages_do_not_replace_missing_selected_uv_with_path(tmp_path, monkeypatch):
    fallback = tmp_path / "uv"
    fallback.write_text("#!/bin/sh\nexit 0\n")
    fallback.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("TOOLFRAME_UV", str(tmp_path / "missing-uv"))
    with pytest.raises(PluginError, match="Configured uv executable is unavailable"):
        Packages(tmp_path / "home", PackageSettings())


def test_python_entry_point_still_discovers_uv_on_path(tmp_path, monkeypatch):
    fallback = tmp_path / "uv"
    fallback.write_text("#!/bin/sh\nexit 0\n")
    fallback.chmod(0o755)
    monkeypatch.delenv("TOOLFRAME_UV", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert Packages(tmp_path / "home", PackageSettings()).uv == str(fallback.resolve())


def test_private_keys_are_not_regenerated_for_an_existing_remote_store(tmp_path):
    from types import SimpleNamespace

    from toolframe_core.access import Access

    access = Access(SimpleNamespace(home=tmp_path))
    definition = SimpleNamespace(id="auth", private_secrets=("password-lookup",))
    with pytest.raises(PluginError, match="Restore the plugin private key"):
        access.private_secrets(definition, store_exists=True)
    assert not (tmp_path / ".toolframe/secrets/auth/password-lookup.key").exists()
    first = access.private_secrets(definition, store_exists=False)
    assert access.private_secrets(definition, store_exists=True) == first


def test_configuration_cas_and_exclusive_host(tmp_path):
    config = ConfigFile(tmp_path)
    config.initialize()
    old = config.revision
    config.put_plugin("example", PluginRequest(source="./example"), old)
    with pytest.raises(PluginError, match="changed"):
        config.put_plugin("other", PluginRequest(source="./other"), old)
    assert set(config.read()[0].plugins) == {"example"}
    first, second = HostLock(tmp_path), HostLock(tmp_path)
    first.acquire()
    try:
        with pytest.raises(PluginError, match="Another core"):
            second.acquire()
    finally:
        first.close()
    second.acquire()
    second.close()


def test_snapshot_tracks_content_and_excludes_runtime_data(tmp_path):
    source, snapshots = tmp_path / "source", tmp_path / "snapshots"
    source.mkdir()
    snapshots.mkdir()
    (source / "pyproject.toml").write_text('[project]\nname="example"\n')
    (source / "plugin.py").write_text("version = 1")
    (source / "data").mkdir()
    (source / "data/private.sqlite").write_text("private")
    (source / ".local").mkdir()
    (source / ".local/runtime.env").write_text("private runtime credentials")
    (source / ".worktrees").mkdir()
    (source / ".worktrees/feature").symlink_to(tmp_path)
    captured, first = snapshot(source, snapshots)
    assert not (captured / "data").exists()
    assert not (captured / ".local").exists()
    assert not (captured / ".worktrees").exists()
    (source / "plugin.py").write_text("version = 2")
    _, second = snapshot(source, snapshots)
    assert first != second
    assert (captured / "plugin.py").read_text() == "version = 1"
    (source / "escape").symlink_to(tmp_path)
    with pytest.raises(PluginError, match="symlinks"):
        snapshot(source, snapshots)


def test_operation_journal_recovers_interruption_and_idempotency(tmp_path):
    state = CoreState(tmp_path)
    operation, created = state.new_operation(
        "reload", "demo", {"version": 1}, "retry-key"
    )
    assert created
    same, created = state.new_operation("reload", "demo", {"version": 1}, "retry-key")
    assert not created and same["id"] == operation["id"]
    with pytest.raises(PluginError):
        state.new_operation("reload", "demo", {"version": 2}, "retry-key")
    state.update_operation(operation["id"], status="running", phase="draining")
    state.close()
    state = CoreState(tmp_path)
    state.reconcile_startup("fresh-host")
    assert state.operation(operation["id"])["status"] == "interrupted"
    state.close()


@pytest.mark.asyncio
async def test_build_and_validate_immutable_local_plugin(tmp_path):
    Path(__file__).resolve().parents[2]
    package = Packages(tmp_path, PackageSettings(find_links=[str(wheelhouse())]))
    source = tmp_path / "sample"
    source.mkdir()
    (source / "pyproject.toml").write_text("""[build-system]
requires = ["hatchling>=1.27,<2"]
build-backend = "hatchling.build"
[project]
name = "toolframe-fixture"
version = "0.1.0"
requires-python = ">=3.14"
dependencies = ["toolframe-sdk>=0.4.1,<1"]
[project.entry-points."toolframe.plugins"]
fixture = "fixture:plugin"
[tool.hatch.build.targets.wheel]
packages = ["fixture"]
""")
    (source / "fixture").mkdir()
    implementation = source / "fixture/__init__.py"
    implementation.write_text(
        "from toolframe_sdk import AuthorizationSpec, ScopeSpec, Plugin, PluginDefinition\n"
        'plugin = PluginDefinition(authorization=AuthorizationSpec(), id="fixture", factory="fixture:Example")\n'
        "class Example(Plugin): pass\n"
    )
    first = await package.prepare("fixture", str(source), {})
    assert first["validation"]["preflight"] == "passed"
    assert first["definition"]["id"] == "fixture"
    assert (tmp_path / first["environment"]["python"]).is_file()
    implementation.write_text(
        implementation.read_text() + '\nMESSAGE = "new same-version code"\n'
    )
    second = await package.prepare("fixture", str(source), {})
    assert second["version"] == first["version"]
    assert second["id"] != first["id"]
    assert second["environment"]["id"] != first["environment"]["id"]
    implementation.write_text("raise RuntimeError('broken candidate')")
    with pytest.raises(PluginError, match="broken candidate"):
        await package.prepare("fixture", str(source), {})
    assert (tmp_path / first["artifact"]).is_file()


async def test_git_branch_wheel_index_and_offline_cached_resolution(tmp_path):
    import asyncio
    import shutil
    import subprocess

    from test_host import fixture_source

    Path(__file__).resolve().parents[2]
    source = fixture_source(tmp_path, "sources")
    for arguments in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "add", "."],
        [
            "git",
            "-c",
            "user.name=Toolframe Tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        ["git", "branch", "feature/plugin"],
    ):
        await asyncio.to_thread(
            subprocess.run, arguments, cwd=source, check=True, capture_output=True
        )
    package = Packages(
        tmp_path / "home",
        PackageSettings(find_links=[str(wheelhouse())]),
    )
    git_source = f"git+{source.as_uri()}@feature/plugin"
    captured = await package.capture(git_source)
    assert len(captured["source"]["commit"]) == 40
    wheel = package.home / captured["artifact"]
    direct = await package.capture("wheel:" + str(wheel))
    assert direct["artifact_hash"] == captured["artifact_hash"]
    package.settings.find_links.append(str(wheel.parent))
    indexed = await package.capture("pypi:fixture-sources==0.1.0")
    assert indexed["plugin_id"] == "sources"
    assert indexed["artifact_hash"] == captured["artifact_hash"]
    package.settings.offline = True
    shutil.rmtree(source)
    assert (await package.capture(git_source))["artifact_hash"] == captured[
        "artifact_hash"
    ]
    assert (await package.capture("pypi:fixture-sources==0.1.0"))[
        "artifact_hash"
    ] == indexed["artifact_hash"]
    with pytest.raises(PluginError, match="Unknown local source"):
        await package.capture(str(source))


async def test_external_state_rejects_scratch_validation_before_worker_start(tmp_path):
    from types import SimpleNamespace

    from toolframe_sdk import AuthorizationSpec, PluginDefinition

    from toolframe_core.host import Host

    definition = PluginDefinition(
        id="external-storage",
        kind="provider",
        provider_kind="sql",
        factory="external:Provider",
        authorization=AuthorizationSpec(),
        runtime_validation="separate_application",
    )
    candidate = {"definition": definition.model_dump()}
    from unittest.mock import AsyncMock

    from toolframe_core.state import StateSettings

    host = SimpleNamespace(
        selected=AsyncMock(return_value={}),
        composition=SimpleNamespace(bindings={}),
        state=SimpleNamespace(settings=StateSettings()),
    )
    with pytest.raises(PluginError, match="separately provisioned test application"):
        await Host.runtime_validate(host, "external-storage", candidate)
