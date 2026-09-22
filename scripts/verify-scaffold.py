#!/usr/bin/env python3
"""Verify plugin new through a copied release binary and real CLI validation."""

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
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
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    original, wheels = args.binary.resolve(), args.wheelhouse.resolve()
    root = Path(tempfile.mkdtemp(prefix="toolframe-scaffold-")).resolve()
    binary = root / "toolframe"
    shutil.copy2(original, binary)
    environment = {**os.environ, "TOOLFRAME_RUNTIME_ROOT": str(root / "runtime")}
    environment.pop("TOOLFRAME_WHEELHOUSE", None)
    environment.pop("PYTHONPATH", None)
    commands = []

    def run(*arguments, expected=0, structured=True):
        completed = subprocess.run(
            [str(binary), *map(str, arguments)],
            env=environment,
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        with (root / "commands.log").open("a") as log:
            log.write(completed.stdout + completed.stderr)
        assert completed.returncode == expected, (
            completed.stderr[-4000:] or completed.stdout
        )
        result = completed.stdout.strip()
        if structured:
            result = json.loads(result or completed.stderr.splitlines()[-1])
        commands.append(
            {
                "arguments": list(map(str, arguments)),
                "exit_code": completed.returncode,
                "result": result,
            }
        )
        return result

    home = root / "app"
    created = run("--home", home, "plugin", "new", "customer-notes", "--json")
    source = home / "plugins/customer-notes"
    assert created == {
        "plugin_id": "customer-notes",
        "package_name": "toolframe-customer-notes",
        "python_module": "toolframe_customer_notes",
        "path": str(source),
        "status": "created",
    }
    assert sorted(path.name for path in home.iterdir()) == ["plugins"]
    assert len(inventory(source)) == 8
    assert (source / "LICENSE").read_text().startswith("MIT License\n")
    custom = root / "custom/source"
    text = run(
        "--offline",
        "--home",
        home,
        "plugin",
        "new",
        "reading-tracker",
        "--output",
        "custom/source",
        structured=False,
    )
    assert str(custom) in text and (custom / "pyproject.toml").exists()
    assert not (custom / "reading-tracker").exists()
    edge = run("--home", home, "plugin", "new", "a--b-", "--json")
    assert edge["package_name"] == "toolframe-a--b-plugin"
    assert not (home / "toolframe.toml").exists()
    assert not (home / ".toolframe").exists()
    assert not (home / ".agents").exists()

    for identifier in ("Bad_ID", "../escape", "a" * 65):
        output = root / "invalid-output"
        result = run(
            "--home",
            home,
            "plugin",
            "new",
            identifier,
            "--output",
            output,
            "--json",
            expected=2,
        )
        assert result["error"]["code"] == "malformed_input"
        assert not output.exists()
    for kind in (
        "file",
        "directory",
        "empty_directory",
        "symlink_file",
        "symlink_directory",
        "dangling_symlink",
    ):
        destination = root / f"existing-{kind}"
        protected = root / f"protected-{kind}"
        if kind == "file":
            destination.write_text("user content")
        elif kind in ("directory", "empty_directory"):
            destination.mkdir()
            if kind == "directory":
                (destination / "keep.txt").write_text("user content")
        elif kind == "symlink_directory":
            protected.mkdir()
            (protected / "keep.txt").write_text("user content")
            destination.symlink_to(protected, target_is_directory=True)
        else:
            if kind == "symlink_file":
                protected.write_text("user content")
            destination.symlink_to(protected)
        before = destination.lstat()
        result = run(
            "--home",
            home,
            "plugin",
            "new",
            "customer-notes",
            "--output",
            destination,
            "--json",
            expected=5,
        )
        assert result["error"]["code"] == "scaffold_conflict"
        assert destination.lstat() == before
    before = inventory(source)
    run("--home", home, "plugin", "new", "customer-notes", "--json", expected=5)
    assert inventory(source) == before

    wheel_reports = []
    for directory in (source, Path(edge["path"])):
        distribution = root / "dist" / directory.name
        completed = subprocess.run(
            ["uv", "build", "--no-sources", "--wheel", "--out-dir", str(distribution)],
            cwd=directory,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        wheel = next(distribution.glob("*.whl"))
        wheel_reports.append(
            {
                "path": str(wheel),
                "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            }
        )
    plugin_wheel = Path(wheel_reports[0]["path"])
    with zipfile.ZipFile(plugin_wheel) as archive:
        entries = archive.read(
            "toolframe_customer_notes-0.1.0.dist-info/entry_points.txt"
        ).decode()
        assert "customer-notes = toolframe_customer_notes.plugin:plugin" in entries
        assert "toolframe_customer_notes/migrations/0001_init.sql" in archive.namelist()
        assert "toolframe_customer_notes/docs/usage.md" in archive.namelist()

    # Only now create a composition and start core for the separate add/validate step.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    initialized = run("--home", home, "init", "--port", port, "--json")
    assert initialized["skill"]["status"] == "installed"
    assert inventory(source) == before
    local_wheels = root / "wheelhouse"
    local_wheels.mkdir()
    for name in ("toolframe_sdk", "toolframe_storage_sqlite"):
        for wheel in wheels.glob(f"{name}-*.whl"):
            shutil.copy2(wheel, local_wheels / wheel.name)
    configuration = home / "toolframe.toml"
    composition_before = configuration.read_bytes()
    with (root / "server.log").open("w") as log:
        server = subprocess.Popen(
            [
                str(binary),
                "--wheelhouse",
                str(local_wheels),
                "--home",
                str(home),
                "serve",
            ],
            env=environment,
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 60
            while True:
                assert server.poll() is None, f"Server exited; see {log.name}"
                assert time.monotonic() < deadline, f"Server not ready; see {log.name}"
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/healthz", timeout=1
                    ) as response:
                        if response.status == 200:
                            break
                except OSError:
                    pass
                time.sleep(0.1)
            status = run("--home", home, "status", "--json")
            assert status["core"] == "ready" and status["plugins"] == []
            run("--home", home, "plugin", "new", "unregistered-notes", "--json")
            after = run("--home", home, "status", "--json")
            assert (
                after["plugins"] == [] and after["operations"] == status["operations"]
            )
            assert configuration.read_bytes() == composition_before
            storage = next(local_wheels.glob("toolframe_storage_sqlite-*.whl"))
            run("--home", home, "plugin", "add", storage, "--enable", "--json")
            added = run("--home", home, "plugin", "add", plugin_wheel, "--json")
            assert added["status"] == "succeeded"
            validation = run(
                "--home", home, "plugin", "validate", "customer-notes", "--json"
            )
            assert validation["status"] == "succeeded"
            runtime = run(
                "--home",
                home,
                "plugin",
                "validate",
                "customer-notes",
                "--runtime",
                "--json",
            )
            assert runtime["status"] == "succeeded"
            plugin_status = run(
                "--home", home, "plugin", "status", "customer-notes", "--json"
            )
            assert plugin_status["desired"]["enabled"] is False, plugin_status
            # Customized source must survive explicit skill refresh.
            customization = source / "custom-business.txt"
            customization.write_text("Owned by the project, not by the skill bundle.\n")
            customized = inventory(source)
            run("--home", home, "skill", "install", "--overwrite", "--json")
            assert inventory(source) == customized
        finally:
            server.terminate()
            try:
                server.wait(timeout=30)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=10)

    report = {
        "binary": str(original),
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "workspace": str(root),
        "commands": commands,
        "wheels": wheel_reports,
        "checks": {
            "copied_binary_without_checkout_or_installed_skill": True,
            "default_output_json_and_explicit_output_text": True,
            "invalid_ids_and_all_existing_target_types_preserved": True,
            "non_sample_and_trailing_hyphen_ids_build_wheels": True,
            "wheel_contains_entry_point_migration_and_document": True,
            "generated_wheel_passes_cli_preflight_and_scratch_runtime": True,
            "scaffold_does_not_modify_composition_register_or_activate": True,
            "skill_install_leaves_customized_plugin_source_untouched": True,
        },
        "passed": True,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Built CLI scaffolding passed; evidence: {args.output}")


if __name__ == "__main__":
    main()
