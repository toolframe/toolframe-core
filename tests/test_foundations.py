from pathlib import Path

import pytest
from toolframe_sdk import PluginError

from toolframe_core.config import ConfigFile, HostLock, PackageSettings, PluginRequest
from toolframe_core.packages import Packages, snapshot
from toolframe_core.state import CoreState


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
    captured, first = snapshot(source, snapshots)
    assert not (captured / "data").exists()
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
    workspace = Path(__file__).resolve().parents[2]
    package = Packages(
        tmp_path, PackageSettings(find_links=[str(workspace / "toolframe-sdk/dist")])
    )
    source = tmp_path / "sample"
    source.mkdir()
    (source / "pyproject.toml").write_text("""[build-system]
requires = ["hatchling>=1.27,<2"]
build-backend = "hatchling.build"
[project]
name = "toolframe-fixture"
version = "0.1.0"
requires-python = ">=3.14"
dependencies = ["toolframe-sdk==0.1.0"]
[project.entry-points."toolframe.plugins"]
fixture = "fixture:plugin"
[tool.hatch.build.targets.wheel]
packages = ["fixture"]
""")
    (source / "fixture").mkdir()
    implementation = source / "fixture/__init__.py"
    implementation.write_text(
        "from toolframe_sdk import Plugin, PluginDefinition\n"
        'plugin = PluginDefinition(id="fixture", factory="fixture:Example")\n'
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
    import shutil
    import subprocess

    from test_host import fixture_source

    workspace = Path(__file__).resolve().parents[2]
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
        subprocess.run(arguments, cwd=source, check=True, capture_output=True)
    package = Packages(
        tmp_path / "home",
        PackageSettings(find_links=[str(workspace / "toolframe-sdk/dist")]),
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
