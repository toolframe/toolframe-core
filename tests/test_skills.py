"""Project-local delivery and the shipped plugin's actual process/storage contract."""

# ruff: noqa: F811 -- shared pytest fixture imported from test_host.
import io
import json
import re
import zipfile
from importlib.resources import as_file
from pathlib import Path
from shutil import copytree

import pytest
from dependencies import source as dependency_source
from test_host import host, operate  # noqa: F401
from toolframe_sdk import PluginError

from toolframe_core import cli, scaffold, skills
from toolframe_core.skills import RECEIPT, SKILL_NAME, bundled_skill, install_skill


@pytest.mark.parametrize("template", [False, True])
def test_init_delivers_complete_skill(tmp_path, capsys, template):
    home = tmp_path / "app"
    arguments = ["--home", str(home), "init", "--json"]
    if template:
        recipe = tmp_path / "composition.toml"
        recipe.write_text("[application]\nid='recipe'\nname='Example'\n")
        arguments.extend(["--template", str(recipe)])
    assert cli.main(arguments) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["configuration"] == str(home / "toolframe.toml")
    assert result["skill"]["status"] == "installed"
    destination = Path(result["skill"]["path"])
    assert destination == home / ".agents/skills" / SKILL_NAME
    with as_file(bundled_skill()) as source:
        for path in source.rglob("*"):
            if path.is_file() and path.relative_to(source).parts[0] != "evals":
                assert (
                    destination / path.relative_to(source)
                ).read_bytes() == path.read_bytes()
    assert not (destination / "evals").exists()
    assert not any(
        name.startswith("evals/")
        for name in json.loads((destination / RECEIPT).read_text())["files"]
    )
    assert not (home / ".toolframe/management.token").exists()


def test_standalone_install_is_idempotent_without_app_or_server(tmp_path, capsys):
    home = tmp_path / "new-app"
    arguments = ["--home", str(home), "skill", "install", "--json"]
    assert cli.main(arguments) == 0
    installed = json.loads(capsys.readouterr().out)
    assert installed["status"] == "installed"
    assert cli.main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "unchanged"
    assert not (home / "toolframe.toml").exists()
    assert not (home / ".toolframe").exists()


@pytest.mark.parametrize("existing", ["edited", "directory", "file", "symlink"])
def test_install_preserves_existing_destination(tmp_path, capsys, existing):
    home = tmp_path / "app"
    destination = home / ".agents/skills" / SKILL_NAME
    if existing == "edited":
        install_skill(home)
        protected = destination / "references/design.md"
        protected.write_text("User-specific design rules\n")
    else:
        destination.parent.mkdir(parents=True)
        if existing == "directory":
            destination.mkdir()
            protected = destination / "local.txt"
            protected.write_text("Keep my existing directory\n")
        elif existing == "file":
            protected = destination
            protected.write_text("Keep my file\n")
        else:
            protected = tmp_path / "unrelated"
            protected.mkdir()
            (protected / "custom.txt").write_text("Keep the symlink target\n")
            destination.symlink_to(protected, target_is_directory=True)
            protected = protected / "custom.txt"
    before = protected.read_bytes()
    assert cli.main(["--home", str(home), "skill", "install", "--json"]) == 5
    assert json.loads(capsys.readouterr().out)["status"] == "preserved"
    assert protected.read_bytes() == before
    output = tmp_path / "separate-copy"
    assert (
        cli.main(
            ["--home", str(home), "skill", "install", "--output", str(output), "--json"]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "installed"
    assert (output / "SKILL.md").is_file()
    assert not (home / ".toolframe").exists()


@pytest.fixture
def mutable_bundle(tmp_path, monkeypatch):
    source = tmp_path / "bundled" / SKILL_NAME
    with as_file(bundled_skill()) as original:
        copytree(original, source)
    # Upgrade scenarios use fixed synthetic versions, independent of the release.
    document = source / "SKILL.md"
    document.write_text(
        re.sub(
            r"^version: .*$",
            'version: "0.1.0"',
            document.read_text(),
            count=1,
            flags=re.MULTILINE,
        )
    )
    monkeypatch.setattr(skills, "bundled_skill", lambda: source)
    monkeypatch.setattr(scaffold, "bundled_skill", lambda: source)
    return source


def bump_bundle(source):
    document = source / "SKILL.md"
    document.write_text(
        document.read_text().replace('version: "0.1.0"', 'version: "0.2.0"')
    )
    (source / "references/design.md").write_text("New release design guidance\n")


@pytest.mark.parametrize("alternate_output", [False, True])
def test_install_excludes_development_evals_from_files_and_receipt(
    tmp_path, mutable_bundle, alternate_output
):
    home = tmp_path / "app"
    nested = mutable_bundle / "evals/nested"
    nested.mkdir(parents=True)
    (nested / "case.json").write_text('{"development": true}\n')
    output = tmp_path / "exported" if alternate_output else None
    installed = install_skill(home, output=output)
    destination = Path(installed["path"])
    receipt = json.loads((destination / RECEIPT).read_text())
    assert not (destination / "evals").exists()
    assert not any(name.startswith("evals/") for name in receipt["files"])
    assert set(receipt["files"]) == {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file() and path.name != RECEIPT
    }
    assert (destination / "assets/sample-plugin/pyproject.toml").is_file()
    assert (destination / "references/plugin-api.md").is_file()
    (nested / "case.json").write_text('{"development": "changed"}\n')
    assert install_skill(home, output=output)["status"] == "unchanged"


@pytest.mark.parametrize("customized", [None, "SKILL.md", "evals/cases.json"])
def test_legacy_eval_install_upgrade_preserves_customizations(
    tmp_path, mutable_bundle, customized
):
    home = tmp_path / "app"
    destination = home / ".agents/skills" / SKILL_NAME
    # Reproduce a pre-exclusion binary's complete copy and receipt.
    legacy_evals = mutable_bundle / "evals"
    legacy_evals.mkdir(exist_ok=True)
    (legacy_evals / "cases.json").write_text("{}\n")
    copytree(mutable_bundle, destination)
    (destination / RECEIPT).write_text(
        json.dumps(
            {
                "format": 1,
                "name": SKILL_NAME,
                "version": "0.1.0",
                "files": skills._hashes(destination),
            }
        )
    )
    if customized:
        (destination / customized).write_text("User customization\n")
    before = skills._hashes(destination, include_receipt=True)
    bump_bundle(mutable_bundle)
    result = install_skill(home, upgrade_only=True)
    if customized:
        assert result["status"] == "preserved" and result["confirmation_required"]
        assert skills._hashes(destination, include_receipt=True) == before
    else:
        assert result["status"] == "updated" and not result["confirmation_required"]
        assert not (destination / "evals").exists()
        assert not any(
            name.startswith("evals/")
            for name in json.loads((destination / RECEIPT).read_text())["files"]
        )
        assert install_skill(home, upgrade_only=True)["status"] == "unchanged"


def test_clean_install_updates_from_new_bundle(tmp_path, mutable_bundle):
    home = tmp_path / "app"
    first = install_skill(home)
    assert first["bundled_version"] == "0.1.0"
    bump_bundle(mutable_bundle)
    updated = install_skill(
        home, confirm=lambda result: pytest.fail("Clean updates need no prompt")
    )
    assert updated["status"] == "updated"
    assert (
        updated["previous_version"] == "0.1.0" and updated["bundled_version"] == "0.2.0"
    )
    assert (
        Path(updated["path"]) / "references/design.md"
    ).read_text() == "New release design guidance\n"
    assert (
        json.loads((Path(updated["path"]) / RECEIPT).read_text())["version"] == "0.2.0"
    )
    assert install_skill(home)["status"] == "unchanged"


def test_skill_upgrade_and_replacement_preserve_generated_plugins(
    tmp_path, mutable_bundle, monkeypatch, capsys
):
    home = tmp_path / "app"
    external = tmp_path / "custom-plugin"
    for arguments in (
        ["customer-notes"],
        ["external-notes", "--output", str(external)],
    ):
        assert (
            cli.main(["--home", str(home), "plugin", "new", *arguments, "--json"]) == 0
        )
    generated = home / "plugins/customer-notes/src/toolframe_customer_notes/service.py"
    generated.write_text(generated.read_text() + "\n# User business customization\n")
    roots = (home / "plugins", external)
    before = {
        p: p.read_bytes() for root in roots for p in root.rglob("*") if p.is_file()
    }
    destination = Path(install_skill(home)["path"])
    bump_bundle(mutable_bundle)
    # The next binary can even ship changed example code; generated code is owned
    # by the project and is not an installed-skill receipt member.
    (
        mutable_bundle / "assets/sample-plugin/src/toolframe_sample_notes/service.py"
    ).write_text("# A different bundled example\n")
    monkeypatch.setattr(cli, "_serve", lambda config, **kwargs: 0)
    assert cli.main(["--home", str(home), "serve"]) == 0
    (destination / "references/design.md").write_text("My custom guidance\n")
    assert (
        cli.main(["--home", str(home), "skill", "install", "--overwrite", "--json"])
        == 0
    )
    assert {
        p: p.read_bytes() for root in roots for p in root.rglob("*") if p.is_file()
    } == before
    assert not (home / "toolframe.toml").exists()


@pytest.mark.parametrize("change", ["edit", "add", "delete", "invalid_receipt"])
def test_changes_require_confirmation_on_upgrade(tmp_path, mutable_bundle, change):
    home = tmp_path / "app"
    destination = Path(install_skill(home)["path"])
    reference = destination / "references/design.md"
    if change == "edit":
        reference.write_text("My local guidance\n")
    elif change == "add":
        (destination / "local.md").write_text("My additional guidance\n")
    elif change == "delete":
        reference.unlink()
    else:
        (destination / RECEIPT).write_text("invalid")
    before = {
        p.relative_to(destination): p.read_bytes()
        for p in destination.rglob("*")
        if p.is_file()
    }
    bump_bundle(mutable_bundle)
    result = install_skill(home)
    assert result["status"] == "preserved" and result["confirmation_required"]
    assert {
        p.relative_to(destination): p.read_bytes()
        for p in destination.rglob("*")
        if p.is_file()
    } == before
    approved = install_skill(home, overwrite=True)
    assert approved["status"] == "updated"
    with zipfile.ZipFile(approved["backup"]) as archive:
        for path, value in before.items():
            assert archive.read(path.as_posix()) == value


def test_interactive_install_asks_before_overwriting(tmp_path, monkeypatch, capsys):
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    home = tmp_path / "app"
    destination = Path(install_skill(home)["path"])
    reference = destination / "references/design.md"
    reference.write_text("Keep unless I agree\n")
    arguments = ["--home", str(home), "skill", "install"]
    monkeypatch.setattr(cli.sys, "stdin", Terminal("n\n"))
    assert cli.main(arguments) == 0
    assert "[y/N]" in capsys.readouterr().err
    assert reference.read_text() == "Keep unless I agree\n"
    monkeypatch.setattr(cli.sys, "stdin", Terminal("yes\n"))
    assert cli.main(arguments) == 0
    assert "[y/N]" in capsys.readouterr().err
    assert reference.read_text() != "Keep unless I agree\n"


def test_serve_refreshes_clean_skill_and_retains_changed_upgrade(
    tmp_path, mutable_bundle, monkeypatch, capsys
):
    home = tmp_path / "app"
    destination = Path(install_skill(home)["path"])
    monkeypatch.setattr(cli, "_serve", lambda config, **kwargs: 0)
    bump_bundle(mutable_bundle)
    assert cli.main(["--home", str(home), "serve"]) == 0
    assert "updated (0.2.0)" in capsys.readouterr().err
    reference = destination / "references/design.md"
    reference.write_text("User rules\n")
    (mutable_bundle / "references/design.md").write_text("Another bundle change\n")
    assert cli.main(["--home", str(home), "serve"]) == 0
    assert "interactively" in capsys.readouterr().err
    assert reference.read_text() == "User rules\n"


def test_confirmation_does_not_overwrite_a_new_concurrent_edit(tmp_path):
    home = tmp_path / "app"
    destination = Path(install_skill(home)["path"])
    reference = destination / "references/design.md"
    reference.write_text("First edit\n")

    def confirm(result):
        reference.write_text("Newer edit during confirmation\n")
        return True

    with pytest.raises(PluginError, match="changed during installation"):
        install_skill(home, confirm=confirm)
    assert reference.read_text() == "Newer edit during confirmation\n"


def test_bad_bundle_guidance_does_not_block_server_startup(
    tmp_path, mutable_bundle, monkeypatch, capsys
):
    document = mutable_bundle / "SKILL.md"
    document.write_text("---\nversion: [broken\n---\n")
    started = []
    monkeypatch.setattr(
        cli, "_serve", lambda config, **kwargs: started.append(config.home) or 0
    )
    home = tmp_path / "app"
    assert cli.main(["--home", str(home), "serve"]) == 0
    assert started == [home]
    assert "invalid YAML" in capsys.readouterr().err


def test_failed_directory_publication_restores_previous_skill(
    tmp_path, mutable_bundle, monkeypatch
):
    home = tmp_path / "app"
    destination = Path(install_skill(home)["path"])
    previous = (destination / "SKILL.md").read_bytes()
    bump_bundle(mutable_bundle)
    rename = Path.rename

    def fail_candidate(path, target):
        if path.name == "candidate":
            raise OSError("Cannot publish candidate")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_candidate)
    with pytest.raises(OSError, match="Cannot publish"):
        install_skill(home)
    assert (destination / "SKILL.md").read_bytes() == previous


async def test_generated_starter_runs_headless_and_retains_data_after_edit(
    host, tmp_path, capsys
):
    assert (
        cli.main(
            [
                "--home",
                str(tmp_path / "authoring"),
                "plugin",
                "new",
                "customer-notes",
                "--json",
            ]
        )
        == 0
    )
    source = Path(json.loads(capsys.readouterr().out)["path"])
    storage = await operate(
        host, "add", source=dependency_source("toolframe-sql-sqlite"), enable=True
    )
    assert storage["status"] == "succeeded", storage
    added = await operate(host, "add", source=str(source))
    assert added["status"] == "succeeded", added
    assert "customer-notes" not in host.runtimes
    enabled = await operate(host, "enable", id="customer-notes")
    assert enabled["status"] == "succeeded", enabled
    assert set(host.runtimes) == {"sql-sqlite", "customer-notes"}
    service = "customer-notes.notes"
    note = await host.call_service(
        "customer-notes", service, "capture", {"body": " First "}
    )
    assert note["body"] == "First"
    with pytest.raises(PluginError, match="nonblank"):
        await host.call_service("customer-notes", service, "capture", {"body": "   "})
    before = {name: runtime.generation for name, runtime in host.runtimes.items()}
    validation = await operate(host, "validate", id="customer-notes", runtime=True)
    assert validation["status"] == "succeeded", validation
    assert host.runtimes["customer-notes"].generation == before["customer-notes"]
    module = source / "src/toolframe_customer_notes/service.py"
    module.write_text(
        module.read_text().replace("body = body.strip()", "body = body.strip().upper()")
    )
    reload = await operate(host, "reload", id="customer-notes")
    assert reload["status"] == "succeeded", reload
    assert host.runtimes["customer-notes"].generation != before["customer-notes"]
    assert host.runtimes["sql-sqlite"].generation == before["sql-sqlite"]
    second = await host.call_service(
        "customer-notes", service, "capture", {"body": "second"}
    )
    assert second["body"] == "SECOND"
    result = await host.call_service("customer-notes", service, "list_notes", {})
    assert result == {"notes": [second, note], "count": 2}
