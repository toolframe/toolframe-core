"""Exercise a copied standard executable through a real server interface."""

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument(
        "--address",
        required=True,
        help="Reachable IP on this server, e.g. its LAN address",
    )
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    source = args.binary.resolve()
    directory = Path(tempfile.mkdtemp(prefix="toolframe-network-"))
    binary = directory / "toolframe"
    shutil.copy2(source, binary)
    env = os.environ.copy()
    for name in (
        "TOOLFRAME_WHEELHOUSE",
        "TOOLFRAME_BUNDLED_WHEELHOUSE",
        "TOOLFRAME_SDK_WHEEL",
        "TOOLFRAME_OFFLINE",
        "PYTHONPATH",
    ):
        env.pop(name, None)

    def cli(*arguments):
        result = subprocess.run(
            [str(binary), *map(str, arguments)],
            cwd=directory,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
        )
        if result.returncode:
            raise RuntimeError(f"CLI failed ({result.returncode}): {result.stderr}")
        return result.stdout

    persisted = directory / "saved-host"
    cli("--home", persisted, "init", "--host", "0.0.0.0")
    assert (
        tomllib.loads((persisted / "toolframe.toml").read_text())["application"]["host"]
        == "0.0.0.0"
    )
    with socket.socket() as reservation:
        reservation.bind(("0.0.0.0", 0))
        port = reservation.getsockname()[1]
    home = directory / "app"
    cli("--home", home, "init", "--template", "entry-register", "--port", port)
    composition = (home / "toolframe.toml").read_bytes()
    assert tomllib.loads(composition.decode())["application"]["host"] == "127.0.0.1"
    base = f"http://{args.address}:{port}"
    report = {
        "binary": str(source),
        "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "tested_address": base,
        "diagnostics": str(directory),
        "checks": [],
    }
    with (directory / "serve.log").open("wb") as log:
        process = subprocess.Popen(
            [str(binary), "--home", str(home), "serve", "--host", "0.0.0.0"],
            cwd=directory,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        with httpx.Client(base_url=base, timeout=10, trust_env=False) as client:
            deadline = time.monotonic() + 90
            token_path = home / ".toolframe/management.token"
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(
                        f"Core exited; inspect {directory / 'serve.log'}"
                    )
                if token_path.exists():
                    token = token_path.read_text().strip()
                    authorization = {"Authorization": f"Bearer {token}"}
                    try:
                        response = client.get(
                            "/_toolframe/v1/status", headers=authorization
                        )
                        response.raise_for_status()
                        status = response.json()
                        if status["ready"]:
                            break
                    except httpx.HTTPError:
                        pass
                time.sleep(0.2)
            else:
                raise TimeoutError(
                    f"LAN startup timed out; inspect {directory / 'serve.log'}"
                )
            assert (home / "toolframe.toml").read_bytes() == composition
            assert (
                json.loads((home / ".toolframe/endpoint.json").read_text())["host"]
                == "0.0.0.0"
            )
            report["checks"].append(
                "init persists host; serve override keeps composition unchanged"
            )
            assert json.loads(cli("--home", home, "status", "--check", "--json"))[
                "ready"
            ]
            local_link = urlsplit(cli("--home", home, "open").strip())
            assert local_link.hostname == "127.0.0.1" and local_link.port == port
            report["checks"].append(
                "management CLI and open resolve wildcard to loopback"
            )
            assert client.get("/healthz").status_code == 200
            for path in ("/_toolframe/v1/status", "/mcp", "/llms.txt"):
                assert client.get(path).status_code == 401, path
            assert client.get("/plugins").status_code == 303
            assert (
                client.get("/healthz", headers={"Host": "attacker.invalid"}).status_code
                == 403
            )
            assert (
                client.get(
                    "/_toolframe/v1/status",
                    headers={**authorization, "Origin": "https://attacker.invalid"},
                ).status_code
                == 403
            )
            report["checks"].append(
                "LAN routes require token; hostile Host and Origin remain rejected"
            )
            login = client.post(
                "/_toolframe/session", data={"token": token}, headers={"Origin": base}
            )
            assert (
                login.status_code == 303
                and "httponly" in login.headers["set-cookie"].lower()
            )
            assert client.get("/plugins").status_code == 200
            assert "entry-register" in client.get("/llms.txt").text
            report["checks"].append(
                "LAN sign-in cookie accesses Dashboard and llms.txt"
            )
            cookies = "; ".join(
                f"{name}={value}" for name, value in client.cookies.items()
            )

            async def websocket_probe():
                url = base.replace("http://", "ws://", 1) + "/_stcore/stream"
                try:
                    async with connect(url, origin=base, proxy=None):
                        raise AssertionError("Anonymous LAN WebSocket was accepted")
                except InvalidStatus as error:
                    assert error.response.status_code == 403
                async with connect(
                    url,
                    origin=base,
                    additional_headers={"Cookie": cookies},
                    subprotocols=["streamlit"],
                    proxy=None,
                ) as websocket:
                    assert websocket.subprotocol == "streamlit"

            asyncio.run(websocket_probe())
            report["checks"].append(
                "real Streamlit WebSocket accepts LAN session and rejects anonymous access"
            )
            mcp = next(
                plugin for plugin in status["plugins"] if plugin["id"] == "fastmcp"
            )
            python = home / mcp["candidate_status"]["environment"]["python"]
            probe = subprocess.run(
                [
                    str(python),
                    "-I",
                    "-c",
                    """
import asyncio, json, sys
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
options = json.load(sys.stdin)
async def main():
    transport = StreamableHttpTransport(options['url'], headers={'Authorization': 'Bearer ' + options['token']})
    async with Client(transport) as client:
        tools = await client.list_tools()
        names = [tool.name for tool in tools]
        assert names == ['entry-register__list_entries'], names
        result = await client.call_tool(names[0], {})
        assert not result.is_error
        print(json.dumps({'tools': names, 'call_succeeded': True}))
asyncio.run(main())
""",
                ],
                input=json.dumps({"url": base + "/mcp", "token": token}),
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
            if probe.returncode:
                raise RuntimeError(probe.stderr)
            report["mcp"] = json.loads(probe.stdout)
            report["checks"].append(
                "real MCP client lists and invokes the read-only tool through LAN"
            )
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    report["passed"] = True
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
