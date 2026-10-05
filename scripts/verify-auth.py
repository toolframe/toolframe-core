"""Exercise a copied standard executable with real LAN HTTP, WS and MCP clients."""

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

CRYPTO_BENCHMARK = """
import json, time, platform, resource
from concurrent.futures import ThreadPoolExecutor
from toolframe_auth.crypto import new_verifier
samples = []
for _ in range(3):
    started = time.perf_counter()
    new_verifier('benchmark-only-secret')
    samples.append(round(time.perf_counter() - started, 4))
single_peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
started = time.perf_counter()
with ThreadPoolExecutor(max_workers=2) as pool:
    list(pool.map(new_verifier, ['budget-example-one', 'budget-example-two']))
print(json.dumps({
    'machine': platform.machine(), 'scrypt_seconds': samples,
    'single_peak_rss_bytes': single_peak,
    'two_hashes_seconds': round(time.perf_counter() - started, 4),
    'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
}))
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--address", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--runtime-root",
        type=Path,
        help="Use a separate launcher cache when testing another architecture",
    )
    args = parser.parse_args()
    source = args.binary.resolve()
    directory = Path(tempfile.mkdtemp(prefix="toolframe-auth-release-"))
    binary = directory / "toolframe"
    shutil.copy2(source, binary)
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("TOOLFRAME_") and k not in {"PYTHONPATH", "PYTHONHOME"}
    }
    if args.runtime_root:
        env["TOOLFRAME_RUNTIME_ROOT"] = str(args.runtime_root.resolve())
    home = directory / "app"
    with socket.socket() as reservation:
        reservation.bind(("0.0.0.0", 0))
        port = reservation.getsockname()[1]

    def cli(*arguments):
        result = subprocess.run(
            [str(binary), "--home", str(home), *map(str, arguments)],
            cwd=directory,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert result.returncode == 0, result.stderr[-2000:] + result.stdout[-2000:]
        return json.loads(result.stdout) if "--json" in arguments else result.stdout

    cli("init", "--template", "entry-register", "--port", port)
    report = {
        "binary": str(source),
        "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "diagnostics": str(directory),
        "address": args.address,
        "checks": [],
    }
    log = (directory / "serve.log").open("wb")
    process = subprocess.Popen(
        [str(binary), "--home", str(home), "serve", "--host", "0.0.0.0"],
        env=env,
        cwd=directory,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    local = httpx.Client(
        base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=40
    )
    remote = httpx.Client(
        base_url=f"http://{args.address}:{port}", trust_env=False, timeout=40
    )

    def get(path, client=local):
        response = client.get(path)
        response.raise_for_status()
        return response.json()

    def post(path, data, client=local):
        response = client.post(path, json=data, headers={"Accept": "application/json"})
        response.raise_for_status()
        return response.json()

    def auth(action, data=None):
        return post(f"/_toolframe/v1/auth/{action}", data or {})

    def create(label, password, scopes, **extra):
        return auth(
            "create",
            {
                "label": label,
                "password": password,
                "scopes": scopes,
                "expected_revision": auth("catalog")["revision"],
                **extra,
            },
        )

    def mode():
        return get("/_toolframe/auth/context", remote)

    def record(check):
        report["checks"].append(check)
        print(check, flush=True)

    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            assert process.poll() is None, (
                f"Core exited; inspect {directory / 'serve.log'}"
            )
            try:
                status = get("/_toolframe/v1/status")
                if len(status["plugins"]) == 7 and all(
                    row["healthy"] for row in status["plugins"]
                ):
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        else:
            raise TimeoutError(
                f"Plugin startup timed out; inspect {directory / 'serve.log'}"
            )
        assert get("/_toolframe/auth/context")["local_control"]
        assert mode()["mode"] == "public" and not mode()["local_control"]
        assert remote.get("/").status_code == 200
        assert remote.get("/llms.txt").status_code == 200
        assert remote.get("/_toolframe/v1/status").status_code == 401
        assert (
            remote.get(
                "/_toolframe/v1/status",
                headers={
                    "Host": f"127.0.0.1:{port}",
                    "X-Forwarded-For": "127.0.0.1",
                    "X-Toolframe-Context": "local",
                },
            ).status_code
            == 401
        )
        assert (
            remote.get("/", headers={"Origin": "https://attacker.invalid"}).status_code
            == 403
        )
        record(
            "Seven bundled plugins start; loopback Local Control, anonymous LAN and spoof/origin rejection"
        )

        read = create("Reader", "release-reader-secret", ["entry-register.read"])
        assert read["access"]["mode"] == "protected"
        assert auth("catalog", {})["policy"]["failure_mode"] == "closed"
        assert remote.get("/").status_code == 303
        assert (
            remote.post(
                "/_toolframe/auth/login", json={"password": "wrong"}
            ).status_code
            == 401
        )
        assert mode()["mode"] == "protected"
        post("/_toolframe/auth/login", {"password": "release-reader-secret"}, remote)
        protected = "/_toolframe/v1/services/entry-register/entry-register.entries/"
        public = "/api/entry-register/entry-register.entries/"
        assert remote.post(protected + "list_entries", json={}).status_code == 200
        assert (
            remote.post(
                protected + "create_entry", json={"label": "Denied", "value": 1}
            ).status_code
            == 403
        )
        assert remote.post("/_toolframe/v1/auth/list", json={}).status_code == 403
        assert remote.post(public + "list_entries", json={}).status_code == 200
        assert (
            remote.post(
                public + "create_entry", json={"label": "Denied", "value": 1}
            ).status_code
            == 403
        )
        assert (
            remote.post(
                "/_toolframe/v1/services/auth/toolframe.auth/list", json={}
            ).status_code
            == 403
        )
        record(
            "Password-only login, read-only enforcement, public API and internal-service boundary"
        )

        async def revoke_websocket():
            url = f"ws://{args.address}:{port}/_stcore/stream"
            cookie = "; ".join(
                f"{key}={value}" for key, value in remote.cookies.items()
            )
            async with connect(
                url,
                origin=f"http://{args.address}:{port}",
                subprotocols=["streamlit"],
                additional_headers={"Cookie": cookie},
                proxy=None,
            ) as ws:
                started = time.monotonic()
                await asyncio.to_thread(
                    auth, "revoke", {"id": read["credential"]["id"]}
                )
                async with asyncio.timeout(4):
                    try:
                        while True:
                            await ws.recv()
                    except ConnectionClosed:
                        pass
                report["websocket_revocation_seconds"] = round(
                    time.monotonic() - started, 3
                )
            try:
                async with connect(
                    url,
                    origin=f"http://{args.address}:{port}",
                    subprotocols=["streamlit"],
                    proxy=None,
                ):
                    raise AssertionError("Protected anonymous WebSocket was accepted")
            except InvalidStatus as error:
                assert error.response.status_code == 403

        asyncio.run(revoke_websocket())
        assert remote.post(protected + "list_entries", json={}).status_code == 401
        assert mode()["mode"] == "protected"
        reused = create(
            "New authorization", "release-reader-secret", [], all_business=True
        )
        assert reused["credential"]["id"] != read["credential"]["id"]
        assert remote.post(protected + "list_entries", json={}).status_code == 401
        post("/_toolframe/auth/login", {"password": "release-reader-secret"}, remote)
        post(
            protected + "create_entry",
            {"label": "Retained across auth changes", "value": 12},
            remote,
        )
        record(
            "Real WebSocket closes on revocation; reused secret issues new authority, old session stays invalid"
        )

        token_hash = hashlib.sha256(
            (home / ".toolframe/management.token").read_bytes()
        ).hexdigest()
        key = home / ".toolframe/secrets/auth/password-lookup.key"
        key_hash = hashlib.sha256(key.read_bytes()).hexdigest()
        cli("plugin", "validate", "auth", "--runtime", "--json")
        cli("plugin", "reload", "auth", "--json")
        assert mode()["mode"] == "protected"
        assert remote.post(protected + "list_entries", json={}).status_code == 200
        assert hashlib.sha256(key.read_bytes()).hexdigest() == key_hash
        record(
            "Scratch runtime validation isolates keys/store; auth reload retains eligible browser session"
        )

        cli("plugin", "disable", "auth", "--json")
        assert mode()["reason"] == "auth_unavailable"
        anonymous = httpx.Client(
            base_url=str(remote.base_url), trust_env=False, timeout=30
        )
        try:
            assert (
                anonymous.post(protected + "list_entries", json={}).status_code == 503
            )
            assert anonymous.post(public + "list_entries", json={}).status_code == 200
            for failure_mode, expected in (("public", 200), ("closed", 503)):
                auth(
                    "policy",
                    {
                        "mode": "protected",
                        "local_control": True,
                        "failure_mode": failure_mode,
                        "expected_revision": auth("catalog", {})["revision"],
                    },
                )
                assert (
                    anonymous.post(protected + "list_entries", json={}).status_code
                    == expected
                )
            assert anonymous.get("/_toolframe/v1/status").status_code == 401
        finally:
            anonymous.close()
        cli("plugin", "enable", "auth", "--json")
        assert mode()["mode"] == "protected"
        auth_runtime = next(
            row
            for row in get("/_toolframe/v1/status")["plugins"]
            if row["id"] == "auth"
        )
        os.kill(auth_runtime["runtime"]["pid"], signal.SIGKILL)
        deadline = time.monotonic() + 8
        while mode()["reason"] != "auth_unavailable" and time.monotonic() < deadline:
            time.sleep(0.1)
        assert mode()["reason"] == "auth_unavailable"
        assert mode()["mode"] == "waiting"
        cli("plugin", "reload", "auth", "--json")
        assert mode()["mode"] == "protected"
        cli("plugin", "disable", "dashboard-auth", "--json")
        assert mode()["reason"] == "auth_unavailable"
        assert mode()["mode"] == "waiting"
        cli("plugin", "enable", "dashboard-auth", "--json")
        assert mode()["mode"] == "protected"
        assert post(protected + "list_entries", {}, remote)["count"] == 1
        assert (
            hashlib.sha256(
                (home / ".toolframe/management.token").read_bytes()
            ).hexdigest()
            == token_hash
        )
        record(
            "Auth/integration disablement and actual worker death preserve closed protection; explicit public fallback and recovery respect saved policy"
        )

        status = get("/_toolframe/v1/status")
        mcp = next(row for row in status["plugins"] if row["id"] == "fastmcp")
        python = home / mcp["candidate_status"]["environment"]["python"]
        code = """
import asyncio,json,sys
from fastmcp import Client
async def main():
    async with Client(sys.argv[1]) as client:
        names=[tool.name for tool in await client.list_tools()]
        assert names==['entry-register__list_entries'],names
        result=await client.call_tool(names[0],{})
        assert not result.is_error
        print(json.dumps({'tools':names,'call_succeeded':True}))
asyncio.run(main())
"""
        probe = subprocess.run(
            [str(python), "-I", "-c", code, str(remote.base_url).rstrip("/") + "/mcp"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert probe.returncode == 0, probe.stderr
        report["mcp"] = json.loads(probe.stdout)
        assert remote.get("/llms.txt").status_code == 200
        record(
            "Real MCP client lists/calls the demo tool on LAN without credentials while Dashboard is protected"
        )

        auth_row = next(row for row in status["plugins"] if row["id"] == "auth")
        python = home / auth_row["candidate_status"]["environment"]["python"]
        benchmark = subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                CRYPTO_BENCHMARK,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert benchmark.returncode == 0, benchmark.stderr
        report["crypto"] = json.loads(benchmark.stdout)
        process.terminate()
        process.wait(timeout=30)
        process = subprocess.Popen(
            [str(binary), "--home", str(home), "serve", "--host", "0.0.0.0"],
            env=env,
            cwd=directory,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            assert process.poll() is None
            try:
                observed = mode()["mode"]
                assert observed != "public", "Protected startup briefly opened access"
                if (
                    observed == "protected"
                    and remote.post(protected + "list_entries", json={}).status_code
                    == 200
                ):
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            raise TimeoutError("Retained session did not survive core restart")
        assert post(protected + "list_entries", {}, remote)["count"] == 1
        record(
            "Full core restart retains session and data without transient public access"
        )
        remote.post("/_toolframe/auth/logout")
        assert remote.post(protected + "list_entries", json={}).status_code == 401
        record("Measured packaged scrypt runtime; logout ends access")
        report["passed"] = True
    finally:
        local.close()
        remote.close()
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log.close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
