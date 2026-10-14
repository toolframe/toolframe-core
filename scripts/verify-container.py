"""Exercise a container through Docker, HTTP and the public Toolframe CLI."""

import argparse
import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def docker(*args, check=True, timeout=300):
    result = subprocess.run(
        ["docker", *map(str, args)], capture_output=True, text=True, timeout=timeout
    )
    if check and result.returncode:
        raise RuntimeError(f"docker {args[0]} failed: {result.stdout}\n{result.stderr}")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--variant", choices=("standard", "distroless"), required=True)
    parser.add_argument(
        "--platform", choices=("linux/amd64", "linux/arm64"), required=True
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    prefix = "toolframe-check-" + uuid.uuid4().hex[:12]
    container, volume = prefix, prefix + "-data"
    report = {"image": args.image, "variant": args.variant, "platform": args.platform}
    info = json.loads(docker("image", "inspect", args.image).stdout)[0]
    report.update(image_id=info["Id"], size_bytes=info["Size"])
    assert info["Config"]["User"] == "65532:65532"
    assert info["Architecture"] == args.platform.split("/")[1]
    assert info["Config"]["Entrypoint"][:2] == ["/usr/bin/tini", "--"]
    run = ["run", "--rm", "--platform", args.platform]
    # A fresh container must have an offline Core, even without any application volume.
    docker(*run, "--network", "none", args.image, "--offline", "--help")
    report["cold_offline_core"] = True
    runtime_probe = (
        "import bz2, ctypes, lzma, os, pathlib, platform, sqlite3, ssl; "
        "assert os.getuid() == 65532; "
        "assert platform.python_version() == '3.14.7'; "
        "assert not pathlib.Path('/data/toolframe.toml').exists(); "
        "assert not pathlib.Path('/data/.toolframe/management.token').exists(); "
        f"assert pathlib.Path('/bin/sh').exists() == {args.variant == 'standard'}; "
        "print(platform.machine())"
    )
    report["runtime_architecture"] = docker(
        *run, "--entrypoint", "/opt/toolframe/python3", args.image, "-c", runtime_probe
    ).stdout.strip()
    # Verify PID 1 adopts and reaps an orphan after its original parent exits.
    reaping_probe = """
import os, pathlib, time
assert pathlib.Path('/proc/1/comm').read_text().strip() == 'tini'
read_fd, write_fd = os.pipe()
child = os.fork()
if child == 0:
    os.close(read_fd)
    grandchild = os.fork()
    if grandchild == 0:
        os.close(write_fd)
        time.sleep(0.2)
        os._exit(0)
    os.write(write_fd, str(grandchild).encode())
    os._exit(0)
os.close(write_fd)
orphan = int(os.read(read_fd, 100))
os.close(read_fd)
os.waitpid(child, 0)
deadline = time.monotonic() + 5
while pathlib.Path(f'/proc/{orphan}').exists():
    assert time.monotonic() < deadline, 'Tini did not reap the orphan'
    time.sleep(0.05)
"""
    docker(
        *run,
        "--entrypoint",
        "/usr/bin/tini",
        args.image,
        "--",
        "/opt/toolframe/python3",
        "-c",
        reaping_probe,
    )
    report["tini_orphan_reaping"] = True
    docker("volume", "create", volume)

    def cli(*arguments):
        return docker(
            "exec", container, "/usr/local/bin/toolframe", "--home", "/data", *arguments
        )

    def api(path, data=None):
        # Read the token inside the container; never put it in argv or evidence.
        script = (
            "import json,urllib.request; from pathlib import Path; "
            "token=Path('/data/.toolframe/management.token').read_text().strip(); "
            f"data={data!r}; "
            f"req=urllib.request.Request('http://127.0.0.1:9876'+{path!r}, "
            "data=json.dumps(data).encode() if data is not None else None, "
            "headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'}); "
            "print(urllib.request.urlopen(req,timeout=10).read().decode())"
        )
        return json.loads(
            docker("exec", container, "/opt/toolframe/python3", "-c", script).stdout
        )

    def wait_ready():
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            running = json.loads(docker("inspect", container).stdout)[0]["State"][
                "Running"
            ]
            if not running:
                raise RuntimeError(
                    docker("logs", container).stdout + docker("logs", container).stderr
                )
            result = docker(
                "exec",
                container,
                "toolframe",
                "--home",
                "/data",
                "status",
                "--json",
                "--check",
                check=False,
            )
            if result.returncode == 0:
                return json.loads(result.stdout)
            if result.stdout:
                status = json.loads(result.stdout)
                operations = status.get("operations", [])
                if operations and operations[0]["status"] in (
                    "failed",
                    "failed_recovered",
                ):
                    raise RuntimeError(json.dumps(operations[0].get("error")))
            time.sleep(1)
        raise TimeoutError("Application did not become ready")

    def stop(label):
        start = time.monotonic()
        docker("stop", "--time", "40", container, timeout=50)
        state = json.loads(docker("inspect", container).stdout)[0]["State"]
        logs = docker("logs", container)
        text = logs.stdout + logs.stderr
        args.output.with_suffix(f".{label}.log").write_text(text)
        assert state["ExitCode"] in (0, 143), state
        assert "Application shutdown complete." in text
        report[label + "_stop_seconds"] = round(time.monotonic() - start, 2)
        report[label + "_exit_code"] = state["ExitCode"]
        docker("rm", container)

    try:
        docker(
            *run,
            "-v",
            f"{volume}:/data",
            args.image,
            "init",
            "--template",
            "entry-register",
        )
        docker(
            "run",
            "-d",
            "--platform",
            args.platform,
            "--name",
            container,
            "-v",
            f"{volume}:/data",
            "-p",
            "127.0.0.1::9876",
            args.image,
        )
        state = wait_ready()
        assert len(state["plugins"]) == 7, state
        assert api("/_toolframe/v1/readiness")["ready"]
        pid1 = docker(
            "exec",
            container,
            "/opt/toolframe/python3",
            "-c",
            "from pathlib import Path; print(Path('/proc/1/comm').read_text().strip())",
        ).stdout.strip()
        assert pid1 == "tini", pid1
        # Execute the actual shell-free Docker HEALTHCHECK.
        docker(
            "exec",
            container,
            "/opt/toolframe/python3",
            "/usr/local/lib/toolframe/healthcheck.py",
        )
        address = docker("port", container, "9876/tcp").stdout.strip()
        endpoint = "http://" + address
        with urllib.request.urlopen(endpoint + "/healthz", timeout=10) as response:
            assert response.status == 200
        with urllib.request.urlopen(endpoint + "/", timeout=10) as response:
            assert "streamlit" in response.read().decode().lower()
        with urllib.request.urlopen(endpoint + "/llms.txt", timeout=10) as response:
            assert "Entry Register" in response.read().decode()
        report["http_dashboard_llms"] = True
        service = "/_toolframe/v1/services/entry-register/entry-register.entries/"
        api(service + "create_entry", {"label": "Container acceptance", "value": -2.5})
        entries = api(service + "list_entries", {})
        assert entries["count"] == 1 and entries["entries"][0]["value"] == -2.5
        # Exercise an independent Streamable HTTP MCP client from outside the container.
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

        def mcp(message):
            request = urllib.request.Request(
                endpoint + "/mcp", data=json.dumps(message).encode(), headers=headers
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    headers["Mcp-Session-Id"] = session
                body = response.read().decode()
                if not body:
                    return None
                if response.headers.get("Content-Type", "").startswith(
                    "text/event-stream"
                ):
                    body = next(
                        line[6:]
                        for line in body.splitlines()
                        if line.startswith("data: ")
                    )
                result = json.loads(body)
                assert "error" not in result, result
                return result.get("result")

        initialized = mcp(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "container-verification", "version": "1"},
                },
            }
        )
        headers["MCP-Protocol-Version"] = initialized["protocolVersion"]
        mcp({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = mcp({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["tools"]
        assert tools, "Template must publish at least one MCP tool"
        report["mcp_tools"] = [tool["name"] for tool in tools]
        operation = api(
            "/_toolframe/v1/operations",
            {"action": "reload", "params": {"id": "entry-register"}},
        )
        deadline = time.monotonic() + 120
        while operation["status"] in ("queued", "running"):
            assert time.monotonic() < deadline, operation
            time.sleep(0.5)
            operation = api("/_toolframe/v1/operations/" + operation["id"])
        assert operation["status"] == "succeeded", operation
        report["plugin_reload"] = True
        stop("online")
        # Recreate the container, retaining only /data. No runtime cache volume or network.
        docker(
            "run",
            "-d",
            "--platform",
            args.platform,
            "--name",
            container,
            "--network",
            "none",
            "-v",
            f"{volume}:/data",
            args.image,
            "--offline",
            "serve",
            "--host",
            "0.0.0.0",
        )
        wait_ready()
        entries = api(service + "list_entries", {})
        assert entries["count"] == 1 and entries["entries"][0]["value"] == -2.5
        cli("status", "--json", "--check")
        report["offline_recreation_retains_data"] = True
        stop("offline")
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2), flush=True)
    finally:
        logs = docker("logs", container, check=False)
        if logs.returncode == 0:
            args.output.with_suffix(".failure.log").write_text(
                logs.stdout + logs.stderr
            )
        docker("rm", "--force", container, check=False)
        docker("volume", "rm", volume, check=False)


if __name__ == "__main__":
    main()
