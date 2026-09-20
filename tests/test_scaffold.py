"""Local source generation, exclusive ownership and packaged template identities."""

import ast
import importlib
import json
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest
from toolframe_sdk import PluginDefinition, PluginError

from toolframe_core import cli, scaffold
from toolframe_core.config import Application, Composition, PluginRequest
from toolframe_core.scaffold import new_plugin
from toolframe_core.skills import bundled_skill


def inventory(directory):
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("json_position", ["root", "plugin", "new"])
def test_cli_defaults_and_json_are_local(tmp_path, monkeypatch, capsys, json_position):
    def unexpected(*args, **kwargs):
        pytest.fail(
            "Scaffolding must not connect, bootstrap a host, or request a token"
        )

    monkeypatch.setattr(cli, "management_token", unexpected)
    monkeypatch.setattr(cli, "_base_url", unexpected)
    monkeypatch.setattr(cli.httpx, "Client", unexpected)
    home = tmp_path / "uninitialized-app"
    arguments = ["--home", str(home)]
    for part in ("root", "plugin", "new"):
        if part != "root":
            arguments.append(part)
        if json_position == part:
            arguments.append("--json")
    arguments.append("customer-notes")
    assert cli.main(arguments) == 0
    assert json.loads(capsys.readouterr().out) == {
        "plugin_id": "customer-notes",
        "package_name": "toolframe-customer-notes",
        "python_module": "toolframe_customer_notes",
        "path": str(home / "plugins/customer-notes"),
        "status": "created",
    }
    assert sorted(path.name for path in home.iterdir()) == ["plugins"]
    assert len(inventory(home)) == 7


def test_cli_exact_relative_output_and_text_preserve_application(
    tmp_path, monkeypatch, capsys
):
    home = tmp_path / "application"
    home.mkdir()
    # Even an invalid composition must not be read or repaired by source generation.
    (home / "toolframe.toml").write_text("not a TOML composition")
    (home / ".toolframe").mkdir()
    (home / ".toolframe/management.token").write_text("existing-token")
    (home / ".toolframe/endpoint.json").write_text("existing-endpoint")
    before = inventory(home)
    monkeypatch.chdir(tmp_path)
    assert (
        cli.main(
            ["--home", str(home), "plugin", "new", "tasks", "--output", "custom/source"]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert (
        "Created plugin tasks" in output and str(tmp_path / "custom/source") in output
    )
    assert (tmp_path / "custom/source/pyproject.toml").is_file()
    assert not (tmp_path / "custom/source/tasks").exists()
    assert inventory(home) == before


@pytest.mark.parametrize(
    "identifier",
    ["", "Bad", "a_b", "a.b", "1abc", "../outside", "x/y", " a", "a\n", "a" * 65],
)
def test_invalid_id_leaves_no_files(tmp_path, capsys, identifier):
    home = tmp_path / "app"
    output = tmp_path / "custom"
    assert (
        cli.main(
            [
                "--home",
                str(home),
                "plugin",
                "new",
                identifier,
                "--output",
                str(output),
                "--json",
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "malformed_input"
    assert not home.exists() and not output.exists()


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
    output = tmp_path / "target"
    protected = tmp_path / "protected"
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
    before = inventory(tmp_path)
    stat = output.lstat()
    assert (
        cli.main(
            [
                "--home",
                str(tmp_path / "app"),
                "plugin",
                "new",
                "customer-notes",
                "--output",
                str(output),
                "--json",
            ]
        )
        == 5
    )
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "scaffold_conflict"
    assert output.lstat() == stat and inventory(tmp_path) == before
    assert not (tmp_path / "app").exists()


def test_concurrent_target_creation_is_preserved(tmp_path, monkeypatch):
    destination = tmp_path / "plugins/customer-notes"
    render = scaffold._render

    def concurrent(*args):
        files = render(*args)
        destination.mkdir(parents=True)
        (destination / "own.txt").write_text("concurrent user source")
        return files

    monkeypatch.setattr(scaffold, "_render", concurrent)
    with pytest.raises(PluginError, match="Refusing to overwrite"):
        new_plugin(tmp_path, "customer-notes")
    assert inventory(destination) == {"own.txt": b"concurrent user source"}


def test_failed_write_cleans_incomplete_target(tmp_path, monkeypatch):
    destination = tmp_path / "plugins/customer-notes"
    neighbor = tmp_path / "plugins/existing"
    neighbor.mkdir(parents=True)
    (neighbor / "own.txt").write_text("keep")
    original_open = Path.open

    def fail_write(path, *args, **kwargs):
        if path.name == "service.py" and args == ("xb",):
            raise OSError("simulated disk failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_write)
    with pytest.raises(OSError, match="disk failure"):
        new_plugin(tmp_path, "customer-notes")
    assert not destination.exists()
    assert inventory(tmp_path) == {"plugins/existing/own.txt": b"keep"}


def test_broken_template_leaves_no_target(tmp_path, monkeypatch):
    def broken(*args):
        raise ValueError("invalid packaged template")

    monkeypatch.setattr(scaffold, "_render", broken)
    with pytest.raises(ValueError, match="invalid packaged template"):
        new_plugin(tmp_path / "app", "customer-notes")
    assert list(tmp_path.iterdir()) == []


def test_uses_packaged_template_and_changes_only_identity_tokens(tmp_path, monkeypatch):
    # A zip resource proves generation does not depend on a source checkout or
    # skill installation. Additional asset content travels from this one source.
    archive_path = tmp_path / "resources.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for relative, content in scaffold._files(bundled_skill()):
            if relative.as_posix().endswith("/service.py"):
                content += b'\nimport toolframe_sample_notes.plugin\nEXAMPLE = "About sample-notes and toolframe_sample_notes"\n'
            archive.writestr(f"bundle/{relative.as_posix()}", content)
        archive.writestr(
            "bundle/assets/sample-plugin/example.txt", b"sample-notes is example prose"
        )
    with zipfile.ZipFile(archive_path) as archive:
        monkeypatch.setattr(
            scaffold, "bundled_skill", lambda: zipfile.Path(archive, "bundle/")
        )
        source = Path(new_plugin(tmp_path, "customer-notes")["path"])
    service = (source / "src/toolframe_customer_notes/service.py").read_text()
    assert "import toolframe_customer_notes.plugin" in service
    assert 'EXAMPLE = "About sample-notes and toolframe_sample_notes"' in service
    assert (source / "example.txt").read_text() == "sample-notes is example prose"


@pytest.mark.parametrize("identifier", ["customer-notes", "z", "a--b-", "a" * 64])
def test_names_resources_and_business_example_remain_consistent(
    tmp_path, monkeypatch, identifier
):
    result = new_plugin(tmp_path, identifier)
    source = Path(result["path"])
    module = result["python_module"]
    document = tomllib.loads((source / "pyproject.toml").read_text())
    project = document["project"]
    expected_package = (
        "toolframe-a--b-plugin" if identifier == "a--b-" else f"toolframe-{identifier}"
    )
    assert project["name"] == expected_package
    assert module == expected_package.replace("-", "_")
    assert project["entry-points"]["toolframe.plugins"] == {
        identifier: f"{module}.plugin:plugin"
    }
    assert document["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        f"src/{module}"
    ]
    assert project["optional-dependencies"]["ui"] == ["streamlit==1.61.0"]
    assert not (source / "src/toolframe_sample_notes").exists()
    monkeypatch.syspath_prepend(str(source / "src"))
    try:
        definition = importlib.import_module(f"{module}.plugin").plugin
        assert isinstance(definition, PluginDefinition)
        assert (
            definition.id == identifier
            and definition.factory == f"{module}.service:Notes"
        )
        assert set(definition.services) == {f"{identifier}.notes"}
        methods = definition.services[f"{identifier}.notes"].methods
        assert (
            set(methods) == {"capture", "list_notes"}
            and methods["list_notes"].read_only
        )
        assert definition.requires["storage"].service == "toolframe.storage.sql"
        resources = {resource.kind: resource for resource in definition.resources}
        assert resources["dashboard.page"].data["entrypoint"] == f"{module}.page:render"
        assert resources["mcp.tool"].data["service"] == f"{identifier}.notes"
        assert resources["llms.document"].data == {
            "package": module,
            "path": "docs/usage.md",
        }
        package = source / "src" / module
        assert (
            definition.store.migrations[0].sql
            == (package / "migrations/0001_init.sql").read_text()
        )
        assert f"`{identifier}__list_notes`" in (package / "docs/usage.md").read_text()
        page = ast.parse((package / "page.py").read_text())
        strings = {
            node.value
            for node in ast.walk(page)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert {
            f"{identifier}.notes",
            f"{identifier}-capture",
            f"{identifier}-body",
        } <= strings
        template = bundled_skill().joinpath(
            "assets/sample-plugin/src/toolframe_sample_notes"
        )
        for relative in ("service.py", "migrations/0001_init.sql"):
            assert (package / relative).read_bytes() == template.joinpath(
                relative
            ).read_bytes()
        Composition(
            application=Application(id="test"),
            plugins={identifier: PluginRequest(source=str(source))},
        )
    finally:
        for name in list(sys.modules):
            if name == module or name.startswith(module + "."):
                del sys.modules[name]
