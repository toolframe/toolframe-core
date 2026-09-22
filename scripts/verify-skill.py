#!/usr/bin/env python3
"""Verify the built executable's authoring bundle outside its source checkout."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path


def inventory(directory):
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    original = args.binary.resolve()
    root = Path(tempfile.mkdtemp(prefix="toolframe-skill-"))
    binary = root / "toolframe"
    shutil.copy2(original, binary)
    environment = {**os.environ, "TOOLFRAME_RUNTIME_ROOT": str(root / "runtime")}
    environment.pop("TOOLFRAME_WHEELHOUSE", None)
    commands = []

    def run(*arguments, expected=0):
        result = subprocess.run(
            [str(binary), *map(str, arguments)],
            env=environment,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        with (root / "commands.log").open("a") as log:
            log.write(result.stdout + result.stderr)
        assert result.returncode == expected, result.stderr[-4000:] or result.stdout
        value = json.loads(result.stdout)
        commands.append(
            {
                "arguments": list(map(str, arguments)),
                "exit_code": result.returncode,
                "result": value,
            }
        )
        return value

    # No wheelhouse or sibling checkout is supplied to the copied executable.
    authoring = root / "authoring"
    installed = run("--home", authoring, "skill", "install", "--json")
    assert installed["status"] == "installed"
    version = installed["bundled_version"]
    skill = Path(installed["path"])
    shipped = inventory(skill)
    assert "SKILL.md" in shipped
    assert "references/design.md" in shipped
    assert "references/plugin-api.md" in shipped
    assert "references/operations.md" in shipped
    assert not (skill / "evals").exists()
    assert not any(name.startswith("evals/") for name in shipped)
    receipt = skill / ".toolframe-install.json"
    assert set(json.loads(receipt.read_text())["files"]) == set(shipped) - {
        receipt.name
    }
    core_wheels = list((root / "runtime").rglob("toolframe_core-*.whl"))
    assert core_wheels
    for core_wheel in core_wheels:
        with zipfile.ZipFile(core_wheel) as archive:
            assert not any(
                name.startswith(
                    "toolframe_core/bundled_skills/toolframe-plugin-design/evals/"
                )
                for name in archive.namelist()
            )
    assert not (authoring / "toolframe.toml").exists()
    assert not (authoring / ".toolframe").exists()
    assert (
        run("--home", authoring, "skill", "install", "--json")["status"] == "unchanged"
    )
    for document in skill.rglob("*.md"):
        for target in re.findall(r"\]\(([^)]+)\)", document.read_text()):
            if "://" not in target:
                resolved = (document.parent / target.split("#")[0]).resolve()
                assert resolved.is_relative_to(skill.resolve()) and resolved.exists(), (
                    target
                )

    edited = skill / "references/design.md"
    edited.write_text("Local design rules\n")
    conflict = run("--home", authoring, "skill", "install", "--json", expected=5)
    assert conflict["status"] == "preserved" and conflict["confirmation_required"]
    assert edited.read_text() == "Local design rules\n"
    exported = root / "exported-skill"
    assert (
        run("--offline", "skill", "install", "--output", exported, "--json")["status"]
        == "installed"
    )
    assert inventory(exported) == shipped

    fresh = root / "fresh-app"
    initialized = run("--home", fresh, "init", "--json")
    assert initialized["skill"]["status"] == "installed"
    assert inventory(Path(initialized["skill"]["path"])) == shipped
    assert not (fresh / ".toolframe/management.token").exists()
    initialized = run("--home", authoring, "init", "--json")
    assert initialized["skill"]["status"] == "preserved"
    assert edited.read_text() == "Local design rules\n"

    approved = run("--home", authoring, "skill", "install", "--overwrite", "--json")
    assert approved["status"] == "updated"
    with zipfile.ZipFile(approved["backup"]) as backup:
        assert backup.read("references/design.md") == b"Local design rules\n"
    assert inventory(skill) == shipped

    # Model an older, untouched release using a valid installation receipt.
    document = skill / "SKILL.md"
    document.write_text(
        document.read_text().replace(f'version: "{version}"', 'version: "0.0.9"')
    )
    edited.write_text("Earlier release guidance\n")
    # Older binaries also installed evaluation fixtures and tracked them.
    (skill / "evals/nested").mkdir(parents=True)
    (skill / "evals/nested/case.json").write_text('{"legacy": true}\n')
    receipt = skill / ".toolframe-install.json"
    prior = json.loads(receipt.read_text())
    prior["version"] = "0.0.9"
    prior["files"] = {
        name: value for name, value in inventory(skill).items() if name != receipt.name
    }
    receipt.write_text(json.dumps(prior, indent=2) + "\n")
    updated = run("--home", authoring, "skill", "install", "--json")
    assert updated["status"] == "updated" and updated["previous_version"] == "0.0.9"
    assert updated["bundled_version"] == version and inventory(skill) == shipped

    sample = root / "plugin-source"
    shutil.copytree(exported / "assets/sample-plugin", sample)
    built = subprocess.run(
        ["uv", "build", "--no-sources", "--wheel", "--out-dir", str(root / "dist")],
        cwd=sample,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert built.returncode == 0, built.stderr
    wheel = next((root / "dist").glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        assert "toolframe_sample_notes/migrations/0001_init.sql" in archive.namelist()
        assert "toolframe_sample_notes/docs/usage.md" in archive.namelist()
        entry = archive.read(
            "toolframe_sample_notes-0.1.0.dist-info/entry_points.txt"
        ).decode()
        assert "sample-notes = toolframe_sample_notes.plugin:plugin" in entry

    report = {
        "binary": str(original),
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "workspace": str(root),
        "commands": commands,
        "skill_files": shipped,
        "starter_wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "checks": {
            "copied_binary_without_source_or_wheelhouse": True,
            "complete_bundle_and_internal_links": True,
            "evals_excluded_from_embedded_wheel_install_and_receipt": True,
            "no_application_or_token_needed": True,
            "idempotent_install": True,
            "customized_reference_preserved": True,
            "confirmed_replacement_with_original_backup": True,
            "unmodified_prior_release_receipt_updates_automatically": True,
            "legacy_eval_files_and_receipt_entries_removed_on_clean_upgrade": True,
            "offline_alternate_output": True,
            "fresh_init_and_preserving_init": True,
            "starter_builds_independently_with_sql_and_docs": True,
        },
        "passed": True,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"Bundled skill delivery passed: {len(shipped)} files; evidence: {args.output}"
    )


if __name__ == "__main__":
    main()
