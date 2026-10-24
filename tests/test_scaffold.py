"""Generic output protection and the provider-free default plugin."""

import importlib
import json
import sys
import tomllib
from pathlib import Path

import pytest
from toolframe_sdk import PluginError

from toolframe_core import authoring, cli
from toolframe_core.config import ConfigFile
from toolframe_core.scaffold import new_plugin, select_template


def inventory(directory):
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


@pytest.fixture
def in_process_basic(monkeypatch):
    # Publication tests bypass only the expensive isolated process. The actual
    # wheel runner and fresh environment are exercised in test_authoring.
    async def execute(packages, dist, request):
        assert request["entrypoint"] == "toolframe_core.authoring:generate"
        authoring.generate(
            destination=Path(request["destination"]), parameters=request["parameters"]
        )

    monkeypatch.setattr(authoring, "execute", execute)


@pytest.mark.parametrize("json_position", ["root", "plugin", "new"])
def test_cli_default_is_basic_without_runtime_side_effects(
    tmp_path, monkeypatch, capsys, in_process_basic, json_position
):
    monkeypatch.setattr(cli, "management_token", lambda *args: pytest.fail("No token"))
    monkeypatch.setattr(cli, "_base_url", lambda *args: pytest.fail("No host"))
    home = tmp_path / "app"
    arguments = ["--home", str(home)]
    for part in ("root", "plugin", "new"):
        if part != "root":
            arguments.append(part)
        if json_position == part:
            arguments.append("--json")
    assert cli.main([*arguments, "customer-notes"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["template"]["id"] == "toolframe-core/basic"
    assert result["path"] == str(home / "plugins/customer-notes")
    assert not (home / "toolframe.toml").exists()
    assert not (home / ".toolframe/management.token").exists()
    assert not (home / ".agents").exists()


@pytest.mark.parametrize(
    "identifier",
    ["", "Bad", "a_b", "a.b", "1abc", "../outside", "x/y", " a", "a\n", "a" * 65],
)
def test_invalid_id_leaves_no_files(tmp_path, capsys, identifier):
    home = tmp_path / "app"
    assert cli.main(["--home", str(home), "plugin", "new", identifier, "--json"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "malformed_input"
    assert not home.exists()


@pytest.mark.parametrize(
    "existing",
    [
        "file",
        "empty_directory",
        "directory",
        "symlink_file",
        "symlink_directory",
        "dangling_symlink",
    ],
)
def test_existing_target_is_never_overwritten(tmp_path, capsys, existing):
    output, protected = tmp_path / "target", tmp_path / "protected"
    if existing in ("directory", "empty_directory"):
        output.mkdir()
        if existing == "directory":
            (output / "own.txt").write_text("user source")
    elif existing == "file":
        output.write_text("user file")
    elif existing == "symlink_directory":
        protected.mkdir()
        (protected / "own.txt").write_text("user source")
        output.symlink_to(protected, target_is_directory=True)
    else:
        if existing == "symlink_file":
            protected.write_text("user file")
        output.symlink_to(protected)
    before, stat = inventory(tmp_path), output.lstat()
    home = tmp_path / "app"
    assert (
        cli.main(
            [
                "--home",
                str(home),
                "plugin",
                "new",
                "notes",
                "--output",
                str(output),
                "--json",
            ]
        )
        == 5
    )
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "scaffold_conflict"
    assert output.lstat() == stat and inventory(tmp_path) == before
    assert not home.exists()


def test_relative_output_and_compatibility_aliases(
    tmp_path, monkeypatch, capsys, in_process_basic
):
    monkeypatch.chdir(tmp_path)
    assert (
        cli.main(
            [
                "--home",
                str(tmp_path / "app"),
                "plugin",
                "new",
                "tasks",
                "--output",
                "custom/source",
            ]
        )
        == 0
    )
    assert str(tmp_path / "custom/source") in capsys.readouterr().out
    assert (tmp_path / "custom/source/pyproject.toml").is_file()
    assert select_template(presentation="web") == "toolframe-web/notes"
    assert (
        select_template(presentation="dashboard")
        == "toolframe-streamlit-dashboard/notes"
    )
    with pytest.raises(PluginError, match="Use --template or --presentation"):
        select_template("third-party/example", "web")


async def test_concurrent_target_creation_is_preserved(tmp_path, monkeypatch):
    destination = tmp_path / "plugins/notes"

    async def concurrent(packages, dist, request):
        authoring.generate(
            destination=Path(request["destination"]), parameters=request["parameters"]
        )
        destination.mkdir(parents=True)
        (destination / "own.txt").write_text("concurrent user source")

    monkeypatch.setattr(authoring, "execute", concurrent)
    with pytest.raises(PluginError, match="Refusing to overwrite"):
        await authoring.new_plugin(ConfigFile(tmp_path), "notes")
    assert inventory(destination) == {"own.txt": b"concurrent user source"}


def test_failed_publication_cleans_only_owned_target(tmp_path, monkeypatch):
    staged, destination, neighbor = (
        tmp_path / "staged",
        tmp_path / "target",
        tmp_path / "neighbor",
    )
    staged.mkdir()
    neighbor.mkdir()
    (staged / "service.py").write_text("payload")
    (neighbor / "keep.txt").write_text("keep")
    original = Path.open

    def failure(path, *args, **kwargs):
        if path == destination / "service.py" and args == ("xb",):
            raise OSError("disk failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failure)
    with pytest.raises(OSError, match="disk failure"):
        authoring.publish(staged, destination)
    assert not destination.exists() and (neighbor / "keep.txt").read_text() == "keep"


def test_generator_symlink_output_is_rejected(tmp_path):
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "external").symlink_to(tmp_path / "missing")
    with pytest.raises(PluginError, match="symlinks"):
        authoring.publish(staged, tmp_path / "output")
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("identifier", ["customer-notes", "z", "a--b-", "a" * 64])
def test_basic_has_only_sdk_service_and_tests(
    tmp_path, monkeypatch, in_process_basic, identifier
):
    result = new_plugin(tmp_path, identifier)
    root = Path(result["path"])
    module = result["python_module"]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    assert project["name"] == result["package_name"]
    assert project["dependencies"] == ["toolframe-sdk==0.4.0+web.1"]
    assert not project.get("optional-dependencies")
    assert project["license"] == "MIT" and (root / "LICENSE").read_text().startswith(
        "MIT License"
    )
    assert project["entry-points"]["toolframe.plugins"] == {
        identifier: f"{module}.plugin:plugin"
    }
    assert (root / "tests/test_service.py").is_file()
    monkeypatch.syspath_prepend(str(root / "src"))
    try:
        definition = importlib.import_module(f"{module}.plugin").plugin
        assert (
            not definition.requires
            and not definition.resources
            and not definition.store
        )
        assert set(definition.services) == {f"{identifier}.echo"}
        assert (
            definition.services[f"{identifier}.echo"].methods["echo"].audience
            == "internal"
        )
    finally:
        for key in tuple(sys.modules):
            if key == module or key.startswith(module + "."):
                del sys.modules[key]
