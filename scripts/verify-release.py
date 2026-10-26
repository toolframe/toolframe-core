#!/usr/bin/env python3
"""Exercise a built launcher and packaged providers in disposable application homes."""

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from verification_workspace import create_workspace, managed_verification


def port():
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


@managed_verification
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument(
        "--uv-path", type=Path, help="Verify an explicitly selected uv installation"
    )
    parser.add_argument(
        "--wheelhouse",
        type=Path,
        help="Optional external wheels; omit to verify the published PyPI installation",
    )
    parser.add_argument("--architecture", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    original = args.binary.resolve()
    wheels = args.wheelhouse.resolve() if args.wheelhouse else None
    root = create_workspace(
        f"toolframe-release-{args.architecture.replace('_', '-')}-",
        evidence=args.output,
    )
    binary = root / "toolframe"
    shutil.copy2(original, binary)
    environment = {**os.environ, "TOOLFRAME_RUNTIME_ROOT": str(root / "runtime")}
    for name in (
        "TOOLFRAME_WHEELHOUSE",
        "TOOLFRAME_BUNDLED_WHEELHOUSE",
        "TOOLFRAME_SDK_WHEEL",
        "TOOLFRAME_OFFLINE",
        "TOOLFRAME_UV",
        "PYTHONPATH",
        "VIRTUAL_ENV",
    ):
        environment.pop(name, None)
    if args.uv_path:
        environment["TOOLFRAME_UV"] = str(args.uv_path.resolve())
    if wheels is None:
        # A previous cache must not hide a missing public dependency.
        environment["UV_CACHE_DIR"] = str(root / "uv-cache")
        environment["UV_PYTHON_INSTALL_DIR"] = str(root / "python")
    wheel_args = ["--wheelhouse", str(wheels)] if wheels else []
    reports = {
        "architecture": args.architecture,
        "binary": str(original),
        "copied_binary": str(binary),
        "published_dependencies": wheels is None,
        "configured_uv": environment.get("TOOLFRAME_UV"),
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "wheel_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(wheels.glob("*.whl"))
        }
        if wheels
        else {},
        "workspace": str(root),
        "cases": [],
    }
    for flag in ("--version", "-V"):
        version = subprocess.run(
            [str(binary), flag],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        assert version.stdout.strip().startswith("toolframe ") and not version.stderr
        assert not (root / "runtime").exists(), "Version queries must not bootstrap"
    reports["version_without_bootstrap"] = version.stdout.strip()

    def command(*arguments, env=None, expected=0):
        result = subprocess.run(
            [str(binary), *wheel_args, *map(str, arguments)],
            cwd=root,
            env=env or environment,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        with (root / "commands.log").open("a") as log:
            log.write(result.stdout + result.stderr)
        if expected == 0 and result.returncode:
            raise RuntimeError(
                f"Launcher command failed ({result.returncode}): {result.stderr[-5000:]}"
            )
        return result

    def request(home, endpoint, path, data=None):
        token = (home / ".toolframe/management.token").read_text().strip()
        req = urllib.request.Request(
            endpoint + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.loads(response.read())

    uninitialized = root / "not-an-application"
    rejected = command("--home", uninitialized, "serve", expected=2)
    assert rejected.returncode == 2, rejected.stderr
    assert json.loads(rejected.stderr.splitlines()[-1])["error"]["code"] == (
        "application_not_initialized"
    )
    assert not uninitialized.exists(), "Rejected startup must not create an app home"
    reports["uninitialized_home"] = "rejected_without_creating_state"

    for template in ("empty", "entry-register"):
        home, listen = root / template, port()
        initialize = ["--home", home, "init", "--port", listen]
        if template != "empty":
            initialize += ["--template", template]
        command(*initialize)
        composition = tomllib.loads((home / "toolframe.toml").read_text())
        identity = composition["application"]["id"]
        if wheels is None:
            assert composition.get("packages", {}).get("find_links", []) == []
        for offline in (False, True):
            arguments = [str(binary), *wheel_args]
            if offline:
                arguments += ["--offline"]
            # Launcher-only options leave the default serve command intact.
            # Restart exactly from the selected app home, without a wrapper.
            endpoint = f"http://127.0.0.1:{listen}"
            with (root / f"{template}-{offline}.log").open("w") as log:
                server = subprocess.Popen(
                    arguments,
                    env=environment,
                    cwd=home,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                )
                try:
                    deadline = time.monotonic() + 300
                    while True:
                        if server.poll() is not None:
                            raise RuntimeError(f"Server exited; see {log.name}")
                        try:
                            status = request(home, endpoint, "/_toolframe/v1/status")
                            if status["application"] == "ready":
                                break
                            failed = [
                                op
                                for op in status["operations"]
                                if op["status"] in ("failed", "failed_recovered")
                            ]
                            if failed:
                                raise RuntimeError(json.dumps(failed))
                        except urllib.error.URLError, FileNotFoundError:
                            pass
                        if time.monotonic() > deadline:
                            raise TimeoutError(f"{template} did not become ready")
                        time.sleep(0.1)
                    assert (
                        status["core"] == "ready"
                        and (home / "data/core.sqlite").is_file()
                    )
                    assert len(status["plugins"]) == (0 if template == "empty" else 7)
                    assert request(home, endpoint, "/_toolframe/v1/readiness")["ready"]
                    assert (
                        tomllib.loads((home / "toolframe.toml").read_text())[
                            "application"
                        ]["id"]
                        == identity
                    )
                    interpreters = list((root / "runtime/core").glob("*/bin/python"))
                    assert len(interpreters) == 1, interpreters
                    inspection = subprocess.run(
                        [
                            str(interpreters[0]),
                            "-I",
                            "-c",
                            (
                                "import importlib.util,json,platform; "
                                "print(json.dumps({'architecture':platform.machine(),"
                                "'python':platform.python_version(),"
                                "'optional_imports':{name:importlib.util.find_spec(name) is not None "
                                "for name in ('streamlit','fastmcp','toolframe_demo_entry_register')}}))"
                            ),
                        ],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    runtime_info = json.loads(inspection.stdout)
                    assert runtime_info[
                        "architecture"
                    ] == args.architecture.removesuffix("-rosetta")
                    assert not any(runtime_info["optional_imports"].values())
                    outcome = {
                        "template": template,
                        "application_id": identity,
                        "startup_command": arguments,
                        "startup_directory": str(home),
                        "offline": offline,
                        "core_environment": runtime_info,
                        "status": {
                            key: status[key]
                            for key in (
                                "host_run",
                                "pid",
                                "desired_revision",
                                "observed_revision",
                                "catalog_epoch",
                                "application",
                                "ready",
                                "degraded",
                            )
                        },
                        "plugins": {
                            item["id"]: {
                                "candidate": item["candidate"],
                                "candidate_status": item["candidate_status"],
                                "runtime": item["runtime"],
                            }
                            for item in status["plugins"]
                        },
                    }
                    command(
                        *(["--offline"] if offline else []),
                        "--home",
                        home,
                        "status",
                        "--json",
                        "--check",
                    )
                    if template != "empty":
                        service = "/_toolframe/v1/services/entry-register/entry-register.entries/"
                        if not offline:
                            created = request(
                                home,
                                endpoint,
                                service + "create_entry",
                                {"label": "Release acceptance", "value": -2.5},
                            )
                            assert created["version"] == 1
                        entries = request(home, endpoint, service + "list_entries", {})
                        assert (
                            entries["count"] == 1
                            and entries["entries"][0]["value"] == -2.5
                        )
                        outcome["retained_entry"] = entries["entries"][0]
                        op = (
                            request(
                                home,
                                endpoint,
                                "/_toolframe/v1/operations",
                                {
                                    "action": "reload",
                                    "params": {"id": "entry-register"},
                                },
                            )
                            if not offline
                            else None
                        )
                        if op:
                            while op["status"] in ("queued", "running"):
                                time.sleep(0.1)
                                op = request(
                                    home,
                                    endpoint,
                                    "/_toolframe/v1/operations/" + op["id"],
                                )
                            assert op["status"] == "succeeded", op
                            after = request(home, endpoint, "/_toolframe/v1/status")
                            by_id = lambda state: {
                                item["id"]: item["runtime"]["pid"]
                                for item in state["plugins"]
                            }
                            assert after["pid"] == status["pid"]
                            assert (
                                by_id(after)["entry-register"]
                                != by_id(status)["entry-register"]
                            )
                            assert (
                                by_id(after)["sql-sqlite"]
                                == by_id(status)["sql-sqlite"]
                            )
                            outcome["reload"] = op
                            outcome["after_reload"] = {
                                "pid": after["pid"],
                                "host_run": after["host_run"],
                                "catalog_epoch": after["catalog_epoch"],
                                "plugins": {
                                    item["id"]: {
                                        "candidate": item["candidate"],
                                        "active_status": item["active_status"],
                                        "runtime": item["runtime"],
                                    }
                                    for item in after["plugins"]
                                },
                            }
                        with urllib.request.urlopen(
                            endpoint + "/llms.txt", timeout=5
                        ) as response:
                            assert "Entry Register" in response.read().decode()
                    reports["cases"].append(outcome)
                    print(
                        f"{args.architecture}: {template}, offline={offline}: passed",
                        flush=True,
                    )
                finally:
                    server.send_signal(signal.SIGTERM)
                    server.wait(timeout=40)
                    assert (
                        server.returncode in (0, -signal.SIGTERM)
                        and "Application shutdown complete."
                        in Path(log.name).read_text()
                    ), f"Server shutdown failed: {server.returncode}"
    cold = {
        **environment,
        "TOOLFRAME_RUNTIME_ROOT": str(root / "missing-runtime"),
        "UV_CACHE_DIR": str(root / "missing-cache"),
        "UV_PYTHON_INSTALL_DIR": str(root / "missing-python"),
    }
    result = command(
        "--offline", "--home", root / "offline-cold", "init", env=cold, expected=1
    )
    assert result.returncode != 0 and any(
        word in result.stderr.lower() for word in ("offline", "cache", "download")
    ), result.stderr
    reports["missing_cache_offline"] = "rejected_without_fallback"
    if args.uv_path:
        assert not (root / "runtime/uv").exists(), (
            "Configured uv must avoid a managed download"
        )
        reports["configured_uv_reused"] = True
    reports["embedded_wheels"] = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((root / "runtime/bundles").glob("*/*/*.whl"))
    }
    assert not reports["embedded_wheels"], (
        "Thin launchers must not extract Python wheels"
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(reports, indent=2) + "\n")
    print(f"Evidence: {args.output}; diagnostic logs: {root}", flush=True)


if __name__ == "__main__":
    main()
