#!/usr/bin/env python3
"""Upgrade a disposable pre-auth application with preserved data and composition intent."""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
import tomlkit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-binary", required=True, type=Path)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--address", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    old, new = args.previous_binary.resolve(), args.binary.resolve()
    directory = Path(tempfile.mkdtemp(prefix="toolframe-auth-upgrade-"))
    home = directory / "app"
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("TOOLFRAME_") and k not in {"PYTHONPATH", "PYTHONHOME"}
    }
    with socket.socket() as reservation:
        reservation.bind(("0.0.0.0", 0))
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
    subprocess.run(
        [str(old), "--home", str(home), "plugin", "new", "untouched-notes"],
        env=env,
        cwd=directory,
        capture_output=True,
        check=True,
        timeout=180,
    )
    source = home / "plugins/untouched-notes"

    def source_hashes():
        return {
            str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in source.rglob("*")
            if p.is_file()
        }

    source_before = source_hashes()
    token_path = home / ".toolframe/management.token"
    report = {
        "previous_binary": str(old),
        "binary": str(new),
        "diagnostics": str(directory),
        "checks": [],
    }

    def serve(binary, label, ready=True):
        log = (directory / f"{label}.log").open("wb")
        process = subprocess.Popen(
            [str(binary), "--home", str(home), "serve", "--host", "0.0.0.0"],
            cwd=directory,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                assert process.poll() is None, f"Server exited; inspect {log.name}"
                try:
                    token = token_path.read_text().strip()
                    client = httpx.Client(
                        base_url=f"http://127.0.0.1:{port}",
                        headers={"Authorization": "Bearer " + token},
                        trust_env=False,
                        timeout=20,
                    )
                    response = client.get("/_toolframe/v1/status")
                    response.raise_for_status()
                    status = response.json()
                    if (ready and status["ready"]) or (
                        not ready
                        and any(op["status"] == "failed" for op in status["operations"])
                    ):
                        return process, log, client, status
                    client.close()
                except httpx.HTTPError, FileNotFoundError:
                    pass
                time.sleep(0.2)
            raise TimeoutError(f"Startup timed out; inspect {log.name}")
        except BaseException:
            stop(process, log)
            raise

    def stop(process, log):
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log.close()

    process, log, client, status = serve(old, "previous")
    try:
        assert len(status["plugins"]) == 5
        response = client.post(
            "/_toolframe/v1/services/entry-register/entry-register.entries/create_entry",
            json={"label": "Pre-auth retained row", "value": 42},
        )
        response.raise_for_status()
    finally:
        client.close()
        stop(process, log)
    token_before = token_path.read_bytes()
    composition = home / "toolframe.toml"
    original = composition.read_bytes()
    assert "access" not in tomlkit.parse(original.decode())
    process, log, client, status = serve(new, "incompatible", ready=False)
    try:
        assert not status["ready"] and status["core"] == "ready"
        assert composition.read_bytes() == original
        report["checks"].append(
            "Old explicit-scope-incompatible packages fail while core management and composition remain intact"
        )
    finally:
        client.close()
        stop(process, log)
    desired = tomlkit.parse(composition.read_text())
    for name, request in desired["plugins"].items():
        version = {"entry-register": "0.2.4", "streamlit-dashboard": "0.4.0"}.get(
            name, "0.2.0"
        )
        request["source"] = request["source"].split("==")[0] + "==" + version
    composition.write_text(tomlkit.dumps(desired))
    process, log, client, status = serve(new, "compatible")
    try:
        assert len(status["plugins"]) == 5 and all(
            row["healthy"] for row in status["plugins"]
        )
        assert "auth" not in {row["id"] for row in status["plugins"]}
        with httpx.Client(
            base_url=f"http://{args.address}:{port}", trust_env=False, timeout=20
        ) as remote:
            assert remote.get("/").status_code == 200
            presentation = remote.get("/_toolframe/auth/context").json()
            assert (
                presentation["mode"] == "public" and not presentation["local_control"]
            )
            assert remote.get("/_toolframe/v1/status").status_code == 401
            data = remote.post(
                "/api/entry-register/entry-register.entries/list_entries", json={}
            ).json()
            assert data["count"] == 1 and data["entries"][0]["value"] == 42
        assert token_path.read_bytes() == token_before
        final = tomlkit.parse(composition.read_text())
        assert set(final["plugins"]) == set(desired["plugins"])
        assert "access" not in final and "auth" not in final["plugins"]
        assert source_hashes() == source_before
        assert (
            'version: "0.3.0"'
            in (home / ".agents/skills/toolframe-plugin-design/SKILL.md").read_text()
        )
        report["checks"].append(
            "Compatible no-auth upgrade opens LAN business access without auto-installing auth; existing data, management token and generated source retained; clean skill updated"
        )
        report["passed"] = True
    finally:
        client.close()
        stop(process, log)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
