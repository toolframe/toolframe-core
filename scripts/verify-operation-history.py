"""Verify journal upgrade, full-history queries and dynamic graph with binaries."""

import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import time
from pathlib import Path

import httpx
from verification_workspace import create_workspace, managed_verification


@managed_verification
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-binary", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path)
    args = parser.parse_args()
    directory = create_workspace("toolframe-history-", evidence=args.output)
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
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    client = httpx.Client(
        base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=30
    )
    report = {
        "binary": str(args.binary.resolve()),
        "sha256": hashlib.sha256(new.read_bytes()).hexdigest(),
        "diagnostics": str(directory),
        "checks": [],
        "passed": False,
    }

    def cli(binary, *arguments):
        return subprocess.run(
            [str(binary), "--home", str(home), *arguments],
            cwd=directory,
            env=env,
            capture_output=True,
            check=True,
            timeout=180,
        )

    def get(path, **params):
        response = client.get("/_toolframe/v1/" + path, params=params)
        response.raise_for_status()
        return response.json()

    def operation(action, params, expected="succeeded"):
        response = client.post(
            "/_toolframe/v1/operations", json={"action": action, "params": params}
        )
        response.raise_for_status()
        identifier = response.json()["id"]
        end = time.monotonic() + 90
        while time.monotonic() < end:
            result = get("operations/" + identifier)
            if result["status"] not in {"queued", "running"}:
                assert result["status"] == expected, result
                return result
            time.sleep(0.05)
        raise TimeoutError(identifier)

    def start(binary, label):
        log = (directory / (label + ".log")).open("wb")
        process = subprocess.Popen(
            [str(binary), "--home", str(home), "serve"],
            cwd=directory,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            end = time.monotonic() + 120
            while time.monotonic() < end:
                assert process.poll() is None, log.name
                try:
                    status = get("status")
                    if (
                        status["ready"]
                        and status["operations"]
                        and not any(
                            row["status"] in {"queued", "running"}
                            for row in status["operations"]
                        )
                    ):
                        return process, log
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            raise TimeoutError(log.name)
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

    cli(old, "init", "--port", str(port))
    process, log = start(old, "previous")
    try:
        for index in range(65):
            if index % 3:
                operation("validate", {"id": "retired-plugin"}, expected="failed")
            else:
                operation("plan", {"action": "apply"})
        with sqlite3.connect(f"file:{home}/data/core.sqlite?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            assert db.execute("PRAGMA user_version").fetchone()[0] == 1
            previous = [
                dict(row)
                for row in db.execute("SELECT * FROM operations ORDER BY created,rowid")
            ]
    finally:
        stop(process, log)
    process, log = start(new, "upgraded")
    try:
        first = get("operations", page=1)
        assert first["total"] > 65 and len(first["items"]) == 10
        rows = []
        for page in range(1, first["pages"] + 1):
            rows.extend(get("operations", page=page, through=first["through"])["items"])
        assert [row["sequence"] for row in rows] == list(range(first["total"], 0, -1))
        by_id = {row["id"]: row for row in rows}
        for number, old_row in enumerate(previous, 1):
            current = by_id[old_row["id"]]
            assert current["sequence"] == number
            for key, value in old_row.items():
                assert current[key] == (
                    json.loads(value)
                    if key in {"request", "result", "error"} and value is not None
                    else value
                )
        record(
            "Schema-1 UUIDs, payloads and chronological order preserved with permanent numbers; history beyond 50 records accessible"
        )
        second = get("operations", page=2, through=first["through"])
        operation("apply", {})
        after = get("operations", page=2, through=first["through"])
        assert second["items"] == after["items"] and after["newer_count"] == 1
        filtered = get(
            "operations",
            page=1,
            page_size=100,
            target="retired-plugin",
            kind="validate",
            status="failed",
        )
        assert filtered["total"] == 43
        assert all(
            row["target"] == "retired-plugin"
            and row["kind"] == "validate"
            and row["status"] == "failed"
            for row in filtered["items"]
        )
        assert get("operations", page=1, target="")["total"] > 0
        record(
            "Combined filters cover complete history, including missing plugin IDs; new arrivals do not shift older pages"
        )
        composition = (home / "toolframe.toml").read_bytes()
        cli(new, "plugin", "new", "history-notes", "--json")
        assert (home / "toolframe.toml").read_bytes() == composition
        assert get("status")["dependency_graph"]["nodes"] == []
        cli(new, "plugin", "add", str(home / "plugins/history-notes"), "--json")
        nodes = get("status")["dependency_graph"]["nodes"]
        node = next(row for row in nodes if row["id"] == "history-notes")
        assert node["state"] == "disabled"
        record(
            "Actual CLI plugin new leaves the graph unchanged; plugin add creates the installed disabled node with its declared dependencies"
        )
        report["passed"] = True
    finally:
        stop(process, log)
        client.close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
