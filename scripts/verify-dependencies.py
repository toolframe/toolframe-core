"""Check graph diagnostics using a standard executable and a disposable app."""

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
from verification_workspace import create_workspace, managed_verification


@managed_verification
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path)
    args = parser.parse_args()
    directory = create_workspace("toolframe-dependencies-", evidence=args.output)
    binary = directory / "toolframe"
    shutil.copy2(args.binary.resolve(), binary)
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
            str(binary),
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
        check=True,
        capture_output=True,
        timeout=180,
    )
    report = {
        "binary": str(args.binary.resolve()),
        "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "diagnostics": str(directory),
        "checks": [],
        "passed": False,
    }
    log = (directory / "serve.log").open("wb")
    process = subprocess.Popen(
        [str(binary), "--home", str(home), "serve"],
        env=env,
        cwd=directory,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    client = httpx.Client(
        base_url=f"http://127.0.0.1:{port}", timeout=40, trust_env=False
    )

    def get(path):
        response = client.get(path)
        response.raise_for_status()
        return response.json()

    def status():
        return get("/_toolframe/v1/status")

    def operation(action, params):
        response = client.post(
            "/_toolframe/v1/operations", json={"action": action, "params": params}
        )
        response.raise_for_status()
        identifier = response.json()["id"]
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            result = get(f"/_toolframe/v1/operations/{identifier}")
            if result["status"] not in {"queued", "running"}:
                assert result["status"] == "succeeded", result
                return result["result"]
            time.sleep(0.2)
        raise TimeoutError(f"Operation timed out: {identifier}")

    def record(message):
        report["checks"].append(message)
        print(message, flush=True)

    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            assert process.poll() is None, (
                f"Core exited; inspect {directory / 'serve.log'}"
            )
            try:
                current = status()
                if len(current["plugins"]) == 7 and all(
                    item["healthy"] for item in current["plugins"]
                ):
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        else:
            raise TimeoutError("Bundled plugins did not become healthy")
        graph = current["dependency_graph"]
        assert graph["schema_version"] == 1
        assert len(graph["nodes"]) == 7 and all(
            node["healthy"] for node in graph["nodes"]
        )
        assert any(
            edge["source"] == "dashboard-auth" and edge["target"] == "auth"
            for edge in graph["edges"]
        )
        assert any(
            edge["source"] == "entry-register" and edge["target"] == "sql-sqlite"
            for edge in graph["edges"]
        )
        assert {
            edge["label"] for edge in graph["edges"] if edge["kind"] == "presentation"
        } == {"dashboard.page", "mcp.tool", "llms.document"}
        record(
            "Seven bundled plugins expose Cordis state, resolved service edges and three presentation kinds"
        )

        before = (home / "toolframe.toml").read_bytes()
        refreshed = status()
        assert refreshed["operations"] == current["operations"]
        assert refreshed["dependency_graph"] == graph
        assert (home / "toolframe.toml").read_bytes() == before
        record(
            "Reading the graph leaves composition, generations and operation history unchanged"
        )

        for name, action in (
            ("sql-sqlite", "reload"),
            ("entry-register", "disable"),
        ):
            plan = operation("plan", {"id": name, "action": action})
            assert plan["affected"] == graph["impacts"][name][action]["affected"]
        record("Read-only reload/disable impact matches real reviewed plans")

        operation(
            "disable",
            {
                "id": "entry-register",
                "plan_id": plan["plan_id"],
                "expected_revision": plan["basis"]["revision"],
            },
        )
        disabled = status()["dependency_graph"]
        node = next(
            node for node in disabled["nodes"] if node["id"] == "entry-register"
        )
        assert node["state"] == "disabled" and not node["healthy"]
        assert node["definition_source"] == "last_candidate"
        assert "entry-register" not in disabled["impacts"]
        assert all(
            edge["state"] == "inactive"
            for edge in disabled["edges"]
            if edge["source"] == "entry-register"
        )
        operation("enable", {"id": "entry-register"})
        assert all(node["healthy"] for node in status()["dependency_graph"]["nodes"])
        record(
            "Disabling retains inactive declarations; enabling restores healthy topology"
        )

        # A LAN peer has no diagnostics authority merely because business access
        # is public. Exercise this in the core auth tests; this probe uses loopback.
        report["versions"] = {node["id"]: node["version"] for node in graph["nodes"]}
        report["passed"] = True
    finally:
        client.close()
        process.terminate()
        try:
            process.wait(timeout=40)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        log.close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
