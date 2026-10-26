"""Named recipes freeze selections; local compositions remain user-owned."""

import hashlib
import json
from shutil import copy2, rmtree

import pytest
import tomlkit
from dependencies import wheelhouse
from test_host import operate
from test_package_selection import make_wheel
from toolframe_sdk import PluginError
from toolframe_sdk import __version__ as SDK_VERSION

from toolframe_core.config import ConfigFile
from toolframe_core.host import Host
from toolframe_core.templates import initialize_template

ASSET = "fixture_recipe/templates/app.toml"


@pytest.fixture
def recipe(tmp_path, monkeypatch):
    directory = tmp_path / "wheels"
    text = """schema_version = 1
[application]
id = "example"
name = "Fixture"
[plugins.choice]
source = "pypi:fixture-choice>=1,<2"
enabled = true
[plugins.pinned]
source = "pypi:fixture-pinned==1.0"
enabled = false
"""
    make_wheel(
        directory, "fixture-recipe", "1.0", plugin="recipe", assets={ASSET: text}
    )
    make_wheel(directory, "fixture-choice", "1.0", plugin="choice")
    make_wheel(directory, "fixture-pinned", "1.0", plugin="pinned")
    (sdk_wheel,) = wheelhouse().glob(f"toolframe_sdk-{SDK_VERSION}-*.whl")
    copy2(sdk_wheel, directory)
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "templates.json").write_text(
        json.dumps({"fixture": {"source": "pypi:fixture-recipe>=1,<2", "asset": ASSET}})
    )
    monkeypatch.setattr("toolframe_core.templates.files", lambda package: catalog)
    monkeypatch.setenv("TOOLFRAME_WHEELHOUSE", str(directory))
    monkeypatch.delenv("TOOLFRAME_OFFLINE", raising=False)
    local = tmp_path / "local.toml"
    local.write_text(text)
    return directory, local


async def test_named_recipe_locks_before_first_serve_and_only_update_reselects(
    tmp_path,
    recipe,
    monkeypatch,
):
    directory, _ = recipe
    config = ConfigFile(tmp_path / "new-app")
    await initialize_template(config, "fixture", name="Fresh application")
    composition, _ = config.read()
    assert composition.application.name == "Fresh application"
    assert composition.plugins["choice"].source == "pypi:fixture-choice==1.0"
    assert composition.plugins["pinned"].source == "pypi:fixture-pinned==1.0"
    lock = json.loads((config.home / "toolframe.lock").read_text())
    assert lock["format"] == 1
    selected = lock["plugins"]["choice"]["last_candidate"]
    assert selected["source"]["requirement"] == "pypi:fixture-choice>=1,<2"
    assert (
        hashlib.sha256((config.home / selected["artifact"]).read_bytes()).hexdigest()
        == selected["artifact_hash"]
    )
    assert f'version = "{SDK_VERSION}"' in selected["environment"]["lock"]

    # A new release between initialization and first serve must have no effect.
    make_wheel(directory, "fixture-choice", "1.1", plugin="choice")

    async def unexpected_capture(*args, **kwargs):
        raise AssertionError("Lifecycle operation must use the retained selection")

    for restart in range(2):
        host = await Host.create(config.home)
        original_capture = host.packages.capture
        monkeypatch.setattr(host.packages, "capture", unexpected_capture)
        try:
            await host.start()
            assert host.runtimes["choice"].candidate["id"] == selected["id"]
            for action in ("validate", "reload"):
                result = await operate(host, action, id="choice")
                assert result["status"] == "succeeded", result
            assert await host.call_service("choice", "choice.service", "echo", {}) == {
                "version": "1.0"
            }
            if restart:
                monkeypatch.setattr(host.packages, "capture", original_capture)
                updated = await operate(host, "update", id="choice")
                assert updated["status"] == "succeeded", updated
                assert host.runtimes["choice"].candidate["version"] == "1.0"
                assert (
                    config.read()[0].plugins["choice"].source
                    == "pypi:fixture-choice==1.1"
                )
                result = await operate(host, "reload", id="choice")
                assert result["status"] == "succeeded", result
                assert await host.call_service(
                    "choice", "choice.service", "echo", {}
                ) == {"version": "1.1"}
                assert (
                    config.read()[0].plugins["pinned"].source
                    == "pypi:fixture-pinned==1.0"
                )
        finally:
            await host.close()


async def test_explicit_local_recipe_keeps_pins_ranges_and_custom_sources(
    tmp_path, recipe
):
    _, local = recipe
    document = tomlkit.parse(local.read_text())
    for name, source in {
        "local": "path:../source",
        "git": "git+https://example.org/plugin.git@main",
        "wheel": "wheel:../custom.whl",
        "url": "https://example.org/plugin.whl#sha256=123",
    }.items():
        document["plugins"][name] = {"source": source, "enabled": False}
    local.write_text(tomlkit.dumps(document))
    config = ConfigFile(tmp_path / "explicit-app")
    await initialize_template(config, str(local))
    composition, _ = config.read()
    assert {name: plugin.source for name, plugin in composition.plugins.items()} == {
        name: plugin["source"] for name, plugin in document["plugins"].items()
    }
    assert not (config.home / "toolframe.lock").exists()


async def test_missing_environment_replays_lock_offline_after_new_dependency_release(
    tmp_path,
    recipe,
    monkeypatch,
):
    directory, _ = recipe
    make_wheel(directory, "fixture-support", "1.0")
    make_wheel(
        directory,
        "fixture-choice",
        "1.0",
        plugin="choice",
        requires=["fixture-support>=1,<2"],
    )
    config = ConfigFile(tmp_path / "restore-app")
    await initialize_template(config, "fixture")
    selected = json.loads((config.home / "toolframe.lock").read_text())["plugins"][
        "choice"
    ]["last_candidate"]
    environment = config.home / selected["environment"]["python"]
    rmtree(environment.parent.parent)
    make_wheel(directory, "fixture-support", "1.1")
    monkeypatch.setenv("TOOLFRAME_OFFLINE", "1")
    host = await Host.create(config.home)

    async def unexpected_resolution(*args, **kwargs):
        raise AssertionError("Restoring an environment must replay its saved lock")

    monkeypatch.setattr(host.packages, "resolve", unexpected_resolution)
    monkeypatch.setattr(host.packages, "capture", unexpected_resolution)
    try:
        await host.start()
        assert host.runtimes["choice"].candidate["id"] == selected["id"]
        from toolframe_core.packages import command

        version = await command(
            environment,
            "-I",
            "-c",
            "from importlib.metadata import version; print(version('fixture-support'))",
        )
        assert version.strip() == "1.0"
    finally:
        await host.close()


async def test_named_recipe_cannot_rewrite_existing_configuration_or_retained_artifacts(
    tmp_path,
    recipe,
):
    _, local = recipe
    config = ConfigFile(tmp_path / "existing-app")
    await initialize_template(config, str(local))
    artifact = config.home / ".toolframe/artifacts/retained/plugin.whl"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"retained plugin artifact")
    lock = config.home / "toolframe.lock"
    lock.write_text('{"format": 1, "plugins": {}}')
    before = {path: path.read_bytes() for path in (config.path, lock, artifact)}
    with pytest.raises(PluginError) as error:
        await initialize_template(config, "fixture")
    assert error.value.code == "configuration_exists"
    assert {path: path.read_bytes() for path in before} == before


async def test_warm_offline_initialization_and_missing_resources(
    tmp_path, recipe, monkeypatch
):
    directory, _ = recipe
    config = ConfigFile(tmp_path / "app")
    await initialize_template(config, "fixture")
    before = json.loads((config.home / "toolframe.lock").read_text())
    # Reinitialize this disposable home with captured artifacts and cached dependencies.
    config.path.unlink()
    monkeypatch.setenv("TOOLFRAME_OFFLINE", "1")
    await initialize_template(config, "fixture")
    after = json.loads((config.home / "toolframe.lock").read_text())
    assert after["plugins"] == before["plugins"]
    config.path.unlink()
    rmtree(config.home / ".toolframe/artifacts")
    with pytest.raises(PluginError) as error:
        await initialize_template(config, "fixture")
    assert error.value.code == "offline_artifact_missing"
    assert not config.path.exists()
    assert directory.exists()


async def test_failed_selection_does_not_publish_partial_composition(tmp_path, recipe):
    directory, _ = recipe
    (directory / "fixture_choice-1.0-py3-none-any.whl").unlink()
    config = ConfigFile(tmp_path / "failed-app")
    with pytest.raises(PluginError) as error:
        await initialize_template(config, "fixture")
    assert error.value.code == "no_compatible_version"
    assert not config.path.exists()
    assert not (config.home / "toolframe.lock").exists()


@pytest.mark.parametrize("failure", ["journal", "lock"])
async def test_persistence_failure_leaves_initialization_retryable(
    tmp_path, recipe, monkeypatch, failure
):
    config = ConfigFile(tmp_path / "persistence-app")

    async def unavailable(*args, **kwargs):
        raise OSError("storage unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(
            "toolframe_core.templates.CoreState.save_candidate"
            if failure == "journal"
            else "toolframe_core.templates.write_lock",
            unavailable,
        )
        with pytest.raises(OSError, match="storage unavailable"):
            await initialize_template(config, "fixture")
    assert not config.path.exists()
    await initialize_template(config, "fixture")
    lock = json.loads((config.home / "toolframe.lock").read_text())
    assert lock["desired_revision"] == config.revision
    assert lock["plugins"]["choice"]["last_candidate"]["version"] == "1.0"
