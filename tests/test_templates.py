"""Named recipe release pins affect new applications, not retained state."""

import json
import zipfile

import pytest
import tomlkit
from dependencies import wheel
from toolframe_sdk import PluginError

from toolframe_core.config import ConfigFile
from toolframe_core.templates import initialize_template

ASSET = "toolframe_demo_entry_register/templates/entry-register.toml"


@pytest.fixture
def entry_recipe(tmp_path, monkeypatch):
    artifact = wheel("toolframe-demo-entry-register")
    monkeypatch.setenv("TOOLFRAME_WHEELHOUSE", str(artifact.parent))
    monkeypatch.setenv("TOOLFRAME_OFFLINE", "1")
    with zipfile.ZipFile(artifact) as archive:
        text = archive.read(ASSET).decode()
    local = tmp_path / "entry-register.toml"
    local.write_text(text)
    return local, tomlkit.parse(text)


async def test_named_recipe_selects_patched_fastmcp_for_new_application(
    tmp_path, entry_recipe
):
    _, original = entry_recipe
    assert original["plugins"]["fastmcp"]["source"] == "pypi:toolframe-fastmcp==0.3.0"
    config = ConfigFile(tmp_path / "new-app")
    await initialize_template(config, "entry-register", name="Fresh application")
    composition, _ = config.read()
    assert composition.plugins["fastmcp"].source == "pypi:toolframe-fastmcp==0.3.1"
    assert composition.application.name == "Fresh application"
    for name, plugin in original["plugins"].items():
        if name != "fastmcp":
            assert composition.plugins[name].source == plugin["source"]
        assert composition.plugins[name].enabled == plugin["enabled"]


async def test_explicit_local_recipe_keeps_its_source_selection(tmp_path, entry_recipe):
    local, original = entry_recipe
    config = ConfigFile(tmp_path / "explicit-app")
    await initialize_template(config, str(local))
    composition, _ = config.read()
    assert {name: plugin.source for name, plugin in composition.plugins.items()} == {
        name: plugin["source"] for name, plugin in original["plugins"].items()
    }


async def test_named_recipe_cannot_rewrite_existing_configuration_or_retained_artifacts(
    tmp_path, entry_recipe
):
    local, _ = entry_recipe
    config = ConfigFile(tmp_path / "existing-app")
    await initialize_template(config, str(local))
    artifact = config.home / ".toolframe/artifacts/retained/fastmcp.whl"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"retained plugin artifact")
    lock = config.home / "toolframe.lock"
    lock.write_text(
        json.dumps(
            {
                "plugins": {
                    "fastmcp": {
                        "version": "0.3.0",
                        "source": "pypi:toolframe-fastmcp==0.3.0",
                        "artifact": str(artifact.relative_to(config.home)),
                    }
                }
            }
        )
    )
    before = {path: path.read_bytes() for path in (config.path, lock, artifact)}
    with pytest.raises(PluginError) as error:
        await initialize_template(config, "entry-register")
    assert error.value.code == "configuration_exists"
    assert {path: path.read_bytes() for path in before} == before
    assert config.read()[0].plugins["fastmcp"].source == "pypi:toolframe-fastmcp==0.3.0"
