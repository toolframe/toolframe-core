"""Metadata-only discovery, isolated third-party execution and output protection."""

import copy
import json
import sys
import zipfile
from importlib.resources import files
from pathlib import Path

import pytest
from toolframe_sdk import PluginError

from toolframe_core import authoring, cli
from toolframe_core.authoring_metadata import MANIFEST, from_wheel, parse_manifest
from toolframe_core.config import ConfigFile, PackageSettings
from toolframe_core.packages import Packages
from toolframe_core.scaffold import describe_templates


def manifest():
    return {
        "schema_version": 1,
        "requires_core": ">=0.8,<0.9",
        "templates": [
            {
                "id": "example",
                "title": "Example project",
                "description": "Generate an example project without a runtime plugin.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "color": {"enum": ["blue", "green"]},
                    },
                    "required": ["name", "color"],
                    "additionalProperties": False,
                },
                "generator": "third_party_author:generate",
            }
        ],
    }


def make_wheel(directory, *, document=None, version="1.0", payload=None, runtime=False):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"third_party_author-{version}-py3-none-any.whl"
    prefix = f"third_party_author-{version}.dist-info/"
    content = {
        prefix
        + "METADATA": f"Metadata-Version: 2.4\nName: third-party-author\nVersion: {version}\nRequires-Python: >=3.14\n",
        prefix
        + "WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        prefix + MANIFEST: json.dumps(document or manifest()),
        "third_party_author/__init__.py": payload
        or """import json
def generate(*, destination, parameters):
    (destination / "project.json").write_text(json.dumps(parameters))
""",
    }
    if runtime:
        content[prefix + "entry_points.txt"] = (
            "[toolframe.plugins]\nevil = third_party_author:plugin\n"
        )
    content[prefix + "RECORD"] = "".join(
        f"{name},,\n" for name in [*content, prefix + "RECORD"]
    )
    with zipfile.ZipFile(path, "w") as archive:
        for name, value in content.items():
            archive.writestr(name, value)
    return path


@pytest.mark.parametrize("runtime", [False, True])
def test_list_and_show_only_read_wheel_metadata(tmp_path, monkeypatch, capsys, runtime):
    marker = tmp_path / "imported"
    wheel = make_wheel(
        tmp_path / "wheels",
        runtime=runtime,
        payload=f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\nraise RuntimeError('must not import')\n",
    )
    home = tmp_path / "uninitialized"

    async def forbidden(*args, **kwargs):
        pytest.fail("Discovery must not create an environment or execute code")

    monkeypatch.setattr(Packages, "environment", forbidden)
    monkeypatch.setattr(authoring, "execute", forbidden)
    monkeypatch.setattr(
        cli, "management_token", lambda *args: pytest.fail("No host/token")
    )
    for action in (["list"], ["show", "third-party-author/example"]):
        args = ["--home", str(home), "template", *action, "--from", f"wheel:{wheel}"]
        assert cli.main([*args, "--json"]) == 0
        result = json.loads(capsys.readouterr().out)
        row = result["templates"][0] if "templates" in result else result
        assert row["distribution"] == "third-party-author" and row["version"] == "1.0"
        assert row["id"] == "third-party-author/example"
        assert row["parameters"] == manifest()["templates"][0]["parameters"]
        assert cli.main(args) == 0
        assert capsys.readouterr().out.strip() == describe_templates(result)
    assert not marker.exists() and "third_party_author" not in sys.modules
    assert not (home / "toolframe.toml").exists()
    assert not (home / ".toolframe/management.token").exists()
    assert not list((home / ".toolframe/environments").iterdir())
    with zipfile.ZipFile(wheel) as archive:
        assert not any(name.endswith("pyproject.toml") for name in archive.namelist())


@pytest.mark.parametrize(
    "change,code",
    [
        (lambda d: d.update(schema_version=2), "authoring_version_incompatible"),
        (lambda d: d.update(schema_version=True), "authoring_version_incompatible"),
        (lambda d: d.update(requires_core=">=999"), "authoring_version_incompatible"),
        (lambda d: d.update(requires_core="bad"), "authoring_manifest_invalid"),
        (
            lambda d: d["templates"].append(copy.deepcopy(d["templates"][0])),
            "template_conflict",
        ),
        (lambda d: d["templates"][0].update(id="../bad"), "authoring_manifest_invalid"),
        (lambda d: d["templates"][0].update(title=""), "authoring_manifest_invalid"),
        (
            lambda d: d["templates"][0].update(generator="side_effect()"),
            "authoring_manifest_invalid",
        ),
        (
            lambda d: d["templates"][0].update(
                parameters={"type": "object", "$ref": "https://invalid/schema"}
            ),
            "authoring_manifest_invalid",
        ),
        (
            lambda d: d["templates"][0].update(
                parameters={"type": "object", "required": False}
            ),
            "authoring_manifest_invalid",
        ),
        (lambda d: d.update(version="1.0"), "authoring_manifest_invalid"),
    ],
)
def test_invalid_manifests_are_clear_errors(change, code):
    document = manifest()
    change(document)
    with pytest.raises(PluginError) as raised:
        parse_manifest(json.dumps(document), distribution="test", version="1.0")
    assert raised.value.code == code


def test_invalid_json_and_python_version():
    with pytest.raises(PluginError, match="UTF-8 JSON"):
        parse_manifest("{", distribution="test", version="1.0")
    with pytest.raises(PluginError) as raised:
        parse_manifest(
            json.dumps(manifest()),
            distribution="test",
            version="1.0",
            requires_python=">=999",
        )
    assert raised.value.code == "authoring_version_incompatible"


async def test_conflicting_versions_require_explicit_source(tmp_path, monkeypatch):
    one = from_wheel(make_wheel(tmp_path / "one"))
    two = from_wheel(make_wheel(tmp_path / "two", version="2.0"))
    monkeypatch.setattr(authoring, "available", lambda config: [one, two])
    with pytest.raises(PluginError, match="Multiple versions"):
        await authoring.select(ConfigFile(tmp_path), "third-party-author/example")


async def test_authoring_discovery_refuses_source_builds(tmp_path, monkeypatch):
    packages = Packages(tmp_path, PackageSettings())
    for source in (
        "path:project",
        "git+https://example.invalid/repo",
        "archive.tar.gz",
    ):
        with pytest.raises(PluginError) as raised:
            await packages.capture(source, authoring=True)
        assert raised.value.code == "authoring_source_invalid"


async def test_third_party_generator_without_runtime_entrypoint_is_isolated(
    tmp_path, monkeypatch
):
    wheel = make_wheel(tmp_path / "wheels")
    home = tmp_path / "app"
    config = ConfigFile(home)
    first = await authoring.new_plugin(
        config,
        "example",
        template="third-party-author/example",
        source=f"wheel:{wheel}",
        parameters={"color": "blue"},
    )
    project = Path(first["path"]) / "project.json"
    assert json.loads(project.read_text()) == {"name": "example", "color": "blue"}
    assert "third_party_author" not in sys.modules
    assert not any(
        (Path(entry) / "third_party_author").exists() for entry in sys.path if entry
    )
    assert (
        not config.path.exists() and not (home / ".toolframe/management.token").exists()
    )
    assert len(list((home / ".toolframe/environments").glob("*/.ready"))) == 1
    before = project.read_bytes()
    updated = make_wheel(
        tmp_path / "updated",
        version="2.0",
        payload='def generate(*, destination, parameters):\n    (destination / "new.txt").write_text("new version")\n',
    )
    second = await authoring.new_plugin(
        config,
        "second",
        template="third-party-author/example",
        source=f"wheel:{updated}",
        parameters={"color": "green"},
    )
    assert (Path(second["path"]) / "new.txt").is_file()
    assert project.read_bytes() == before
    with pytest.raises(PluginError) as raised:
        await authoring.new_plugin(
            config,
            "invalid",
            template="third-party-author/example",
            source=f"wheel:{wheel}",
            parameters={"color": "red"},
        )
    assert raised.value.code == "template_parameters_invalid"
    assert not (home / "plugins/invalid").exists()


def test_core_package_contains_only_basic_template():
    core = files("toolframe_core")
    assert core.joinpath("basic_template/pyproject.toml").is_file()
    assert not core.joinpath("web_template").is_dir()
    assert not core.joinpath(
        "bundled_skills/toolframe-plugin-design/assets/sample-plugin"
    ).is_dir()
    paths = [str(path) for path in Path(str(core)).rglob("*")]
    assert not any(
        "toolframe_web_notes" in path or "toolframe_sample_notes" in path
        for path in paths
    )


async def test_wheel_named_directory_and_direct_requirements_never_build(
    tmp_path, monkeypatch
):
    packages = Packages(tmp_path, PackageSettings())
    (tmp_path / "source.whl").mkdir()

    async def forbidden(*args, **kwargs):
        pytest.fail("Discovery executed a source build")

    monkeypatch.setattr(packages, "_build", forbidden)
    for source in (
        "path:source.whl",
        "pypi:example @ https://example.invalid/source.tar.gz",
        "pypi:example[extra]==1.0",
    ):
        with pytest.raises(PluginError) as raised:
            await packages.capture(source, authoring=True)
        assert raised.value.code == "authoring_source_invalid"


async def test_index_discovery_reads_only_local_matching_wheel_and_caches(tmp_path):
    marker = tmp_path / "executed"
    wheel = make_wheel(
        tmp_path / "wheels",
        payload=f"from pathlib import Path\nPath({str(marker)!r}).touch()\nraise RuntimeError('do not import')\n",
    )
    packages = Packages(
        tmp_path / "app", PackageSettings(find_links=[str(wheel.parent)], offline=True)
    )
    first = await authoring.acquire(packages, "pypi:third-party-author==1.0")
    assert first.version == "1.0" and not marker.exists()
    assert not list((packages.root / "environments").iterdir())
    wheel.unlink()
    again = await authoring.acquire(packages, "pypi:third-party-author==1.0")
    assert again.source["sha256"] == first.source["sha256"]


def test_help_never_scans_installed_packages(monkeypatch):
    monkeypatch.setattr(
        authoring,
        "installed_distributions",
        lambda: pytest.fail("Help scanned distributions"),
    )
    with pytest.raises(SystemExit) as raised:
        cli.main(["template", "list", "--help"])
    assert raised.value.code == 0


async def test_unresolvable_parameter_reference_has_clear_error(tmp_path):
    document = manifest()
    document["templates"][0]["parameters"]["properties"]["name"] = {
        "$ref": "#/$defs/missing"
    }
    wheel = make_wheel(tmp_path / "wheels", document=document)
    with pytest.raises(PluginError) as raised:
        await authoring.new_plugin(
            ConfigFile(tmp_path / "app"),
            "example",
            template="third-party-author/example",
            source=f"wheel:{wheel}",
            parameters={"color": "blue"},
        )
    assert raised.value.code == "authoring_manifest_invalid"
    assert not (tmp_path / "app/plugins/example").exists()
