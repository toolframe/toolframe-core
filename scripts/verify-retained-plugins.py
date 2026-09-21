"""Upgrade a disabled bundled plugin across releases using real executables."""

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
import tomlkit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-binary", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path)
    args = parser.parse_args()
    directory = Path(tempfile.mkdtemp(prefix="toolframe-retained-"))
    old, new = directory / "previous", directory / "toolframe"
    shutil.copy2(args.previous_binary.resolve(), old)
    shutil.copy2(args.binary.resolve(), new)
    home = directory / "app"
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("TOOLFRAME_") and key not in {"PYTHONHOME", "PYTHONPATH"}
    }
    if args.runtime_root:
        env["TOOLFRAME_RUNTIME_ROOT"] = str(args.runtime_root.resolve())
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    subprocess.run(
        [
            str(old),
            "--home",
            str(home),
            "init",
            "--template",
            "entry-register",
            "--port",
            str(port),
        ],
        env=env,
        cwd=directory,
        capture_output=True,
        check=True,
        timeout=180,
    )
    report = {
        "previous_binary": str(args.previous_binary.resolve()),
        "binary": str(args.binary.resolve()),
        "sha256": hashlib.sha256(new.read_bytes()).hexdigest(),
        "diagnostics": str(directory),
        "checks": [],
        "passed": False,
    }
    client = httpx.Client(
        base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=30
    )

    def get(path):
        response = client.get("/_toolframe/v1/" + path)
        response.raise_for_status()
        return response.json()

    def operation(action, **params):
        response = client.post(
            "/_toolframe/v1/operations", json={"action": action, "params": params}
        )
        response.raise_for_status()
        identifier = response.json()["id"]
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            result = get("operations/" + identifier)
            if result["status"] not in {"queued", "running"}:
                assert result["status"] == "succeeded", result
                return result["result"]
            time.sleep(0.2)
        raise TimeoutError(identifier)

    def reviewed(action):
        before = (home / "toolframe.toml").read_bytes()
        params = {} if action == "apply" else {"id": "entry-register"}
        response = client.post(
            "/_toolframe/v1/operations",
            json={"action": "plan", "params": {"action": action, **params}},
        )
        response.raise_for_status()
        identifier = response.json()["id"]
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            pending = get("operations/" + identifier)
            if pending["status"] not in {"queued", "running"}:
                assert pending["status"] == "succeeded", pending
                break
            time.sleep(0.2)
        else:
            raise TimeoutError(identifier)
        assert (home / "toolframe.toml").read_bytes() == before
        plan = pending["result"]
        operation(
            action,
            **params,
            plan_id=plan["plan_id"],
            expected_revision=plan["basis"]["revision"],
        )

    def start(binary, label, count):
        log = (directory / (label + ".log")).open("wb")
        process = subprocess.Popen(
            [str(binary), "--home", str(home), "serve"],
            env=env,
            cwd=directory,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                assert process.poll() is None, f"Core exited: {log.name}"
                try:
                    status = get("status")
                    if sum(
                        bool(row["healthy"]) for row in status["plugins"]
                    ) == count and not any(
                        op["status"] in {"queued", "running"}
                        for op in status["operations"]
                    ):
                        return process, log, status
                except httpx.HTTPError:
                    pass
                time.sleep(0.2)
            raise TimeoutError(f"Startup: {log.name}")
        except BaseException:
            stop(process, log)
            raise

    def stop(process, log):
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        log.close()

    def record(message):
        report["checks"].append(message)
        print(message, flush=True)

    def row(status):
        return next(
            item for item in status["plugins"] if item["id"] == "entry-register"
        )

    process, log, status = start(old, "previous", 7)
    try:
        original = row(status)
        response = client.post(
            "/_toolframe/v1/services/entry-register/entry-register.entries/create_entry",
            json={"label": "Retained upgrade row", "value": 42},
        )
        response.raise_for_status()
        reviewed("disable")
        assert not row(get("status"))["desired"]["enabled"]
        record(
            "Old executable installs seven plugins, saves business data and disables Entry Register"
        )
    finally:
        stop(process, log)
    before = (home / "toolframe.toml").read_bytes()
    token_before = (home / ".toolframe/management.token").read_bytes()
    process, log, status = start(new, "upgraded", 6)
    try:
        assert (home / "toolframe.toml").read_bytes() == before
        assert not row(status)["desired"]["enabled"]
        reviewed("enable")
        reviewed("reload")
        operation("validate", id="entry-register")
        reviewed("apply")
        current = row(get("status"))
        assert current["healthy"] and current["active"] == original["active"]
        assert (
            current["active_status"]["version"] == original["active_status"]["version"]
        )
        response = client.post(
            "/api/entry-register/entry-register.entries/list_entries", json={}
        )
        response.raise_for_status()
        entries = response.json()
        assert entries["count"] == 1 and entries["entries"][0]["value"] == 42
        final = tomlkit.parse((home / "toolframe.toml").read_text())
        assert (
            final["plugins"]["entry-register"]["source"]
            == original["desired"]["source"]
        )
        assert final.get("packages", {}).get("find_links", []) == []
        assert (home / ".toolframe/management.token").read_bytes() == token_before
        record(
            "New executable previews/enables/reloads/validates/applies the retained old package with unchanged version, wheel identity, source pin and business data"
        )
        operation("disable", id="entry-register")
        operation("enable", id="entry-register")
        operation("reload", id="entry-register")
        assert row(get("status"))["healthy"]
        record(
            "Direct lifecycle operations also restore the disabled plugin without a package update"
        )
        report["retained_version"] = current["active_status"]["version"]
        report["passed"] = True
    finally:
        stop(process, log)
        client.close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
