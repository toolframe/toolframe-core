import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from toolframe_core import api, cli
from toolframe_core.config import ConfigFile
from toolframe_core.host import Host
from toolframe_core.network import trusted
from toolframe_core.templates import initialize_template


@pytest.mark.parametrize(
    "address",
    ["localhost", "127.0.0.1", "0.0.0.0", "192.168.1.10", "::1", "::", "fd00::10"],
)
def test_config_accepts_local_lan_and_wildcard_addresses(tmp_path, address):
    config = ConfigFile(tmp_path)
    config.initialize(host=address)
    assert config.read()[0].application.host == address


@pytest.mark.parametrize(
    "address",
    [
        "",
        "http://127.0.0.1",
        "192.168.1.10:9876",
        "[::1]",
        "127.0.0.1/path",
        "example.com",
        "224.0.0.1",
    ],
)
def test_invalid_bind_addresses_do_not_create_composition(tmp_path, address):
    config = ConfigFile(tmp_path / "new")
    with pytest.raises(ValueError):
        config.initialize(host=address)
    assert not config.home.exists()


def test_init_host_option_is_persisted(tmp_path, capsys):
    assert cli.main(["--home", str(tmp_path), "init", "--host", "0.0.0.0"]) == 0
    assert ConfigFile(tmp_path).read()[0].application.host == "0.0.0.0"


@pytest.mark.parametrize("override,expected", [(None, "::"), ("0.0.0.0", "0.0.0.0")])
async def test_template_host_is_preserved_or_explicitly_overridden(
    tmp_path, override, expected
):
    template = tmp_path / "template.toml"
    template.write_text('[application]\nid="template"\nhost="::"\n')
    config = ConfigFile(tmp_path / "app")
    await initialize_template(config, str(template), host=override)
    assert config.read()[0].application.host == expected


@pytest.mark.parametrize(
    "address,expected",
    [("0.0.0.0", "127.0.0.1"), ("::", "[::1]"), ("192.168.1.10", "192.168.1.10")],
)
def test_cli_endpoint_uses_connectable_address(tmp_path, capsys, address, expected):
    config = ConfigFile(tmp_path)
    config.initialize(host=address, port=8765)
    assert cli._base_url(config) == f"http://{expected}:8765"
    endpoint = tmp_path / ".toolframe/endpoint.json"
    endpoint.parent.mkdir()
    endpoint.write_text(json.dumps({"host": address, "port": 9876}))
    config.path.write_text("invalid = [")
    assert cli.main(["--home", str(tmp_path), "open"]) == 0
    assert capsys.readouterr().out.startswith(
        f"http://{expected}:9876/_toolframe/session?"
    )


def test_serve_override_binds_and_records_endpoint_without_rewriting_toml(
    tmp_path, monkeypatch, capsys
):
    config = ConfigFile(tmp_path)
    config.initialize()
    before = config.path.read_bytes()
    bindings = []

    def serve(app, **kwargs):
        bindings.append(kwargs)

        async def lifespan():
            async with app.router.lifespan_context(app):
                endpoint = json.loads(
                    (tmp_path / ".toolframe/endpoint.json").read_text()
                )
                assert endpoint == {"host": "0.0.0.0", "port": 9876}

        asyncio.run(lifespan())

    monkeypatch.setattr(cli.uvicorn, "run", serve)
    assert cli.main(["--home", str(tmp_path), "serve", "--host", "0.0.0.0"]) == 0
    assert bindings[0]["host"] == "0.0.0.0"
    assert config.path.read_bytes() == before
    assert "Toolframe: http://127.0.0.1:9876" in capsys.readouterr().out


async def test_runtime_override_survives_composition_refresh(tmp_path):
    config = ConfigFile(tmp_path)
    config.initialize()
    host = Host(tmp_path, listen_host="::")
    await host.start()
    try:
        host.refresh()
        assert host.listen_host == "::"
        assert host.composition.application.host == "127.0.0.1"
        assert host.status()["configuration_error"] is None
    finally:
        await host.close()


@pytest.mark.parametrize(
    "listener,authority,allowed",
    [
        ("127.0.0.1", "192.168.1.10:9876", False),
        ("localhost", "192.168.1.10:9876", False),
        ("0.0.0.0", "192.168.1.10:9876", True),
        ("::", "[fd00::10]:9876", True),
        ("192.168.1.10", "192.168.1.10:9876", True),
        ("192.168.1.10", "192.168.1.11:9876", False),
        ("0.0.0.0", "localhost:9876", True),
        ("0.0.0.0", "attacker.example:9876", False),
        ("0.0.0.0", "attacker@192.168.1.10:9876", False),
        ("0.0.0.0", "192.168.1.10:99999", False),
        ("0.0.0.0", "192.168.1.10:bad", False),
        ("0.0.0.0", "192.168.1.10/path", False),
        ("0.0.0.0", "[::invalid]", False),
    ],
)
def test_host_admission(listener, authority, allowed):
    assert trusted({"host": authority}, listener) is allowed


async def test_lan_http_auth_session_and_origin_checks(tmp_path, monkeypatch):
    ConfigFile(tmp_path).initialize(host="0.0.0.0")
    host = Host(tmp_path)
    await host.start()
    original_client = httpx.AsyncClient

    def upstream(request):
        if request.url.path == "/mcp":
            scheme, reference = request.headers["authorization"].split(" ", 1)
            assert scheme == "Bearer" and reference != host.token
            assert host.mcp_access.requests[reference].credential_id is None
        else:
            assert "authorization" not in request.headers
        if "origin" in request.headers:
            assert request.headers["origin"] == "http://127.0.0.1:12345"
        return httpx.Response(200, stream=httpx.ByteStream(b"provider response"))

    def provider(host, path):
        kind = "mcp" if path == "mcp" else "llms" if path == "llms.txt" else "dashboard"
        return kind, SimpleNamespace(name=kind), "http://127.0.0.1:12345"

    monkeypatch.setattr(api, "provider_for", provider)
    monkeypatch.setattr(
        api.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(upstream)),
    )
    try:
        async with original_client(
            transport=httpx.ASGITransport(
                api.create_app(host, manage_lifespan=False),
                client=("192.168.1.20", 54321),
            ),
            base_url="http://192.168.1.10:9876",
        ) as client:
            assert (await client.get("/healthz")).status_code == 200
            assert (await client.get("/_toolframe/v1/status")).status_code == 401
            for path in ("/mcp", "/llms.txt", "/plugins"):
                assert (await client.get(path)).status_code == 200
            # A loopback Host never promotes a remote socket to Local Control.
            assert (
                await client.get(
                    "/_toolframe/v1/status", headers={"Host": "127.0.0.1:9876"}
                )
            ).status_code == 401
            authorization = {"Authorization": f"Bearer {host.token}"}
            assert (
                await client.get("/_toolframe/v1/status", headers=authorization)
            ).status_code == 200
            assert (
                await client.get("/mcp", headers=authorization)
            ).text == "provider response"
            assert not host.mcp_access.requests
            assert (
                await client.get("/healthz", headers={"Host": "attacker.example"})
            ).status_code == 403
            assert (
                await client.get(
                    "/mcp",
                    headers={**authorization, "Origin": "https://attacker.example"},
                )
            ).status_code == 403
            assert (
                await client.get(
                    "/mcp", headers={**authorization, "Sec-Fetch-Site": "cross-site"}
                )
            ).status_code == 403
            login = await client.post(
                "/_toolframe/session",
                data={"token": host.token},
                headers={"Origin": "http://192.168.1.10:9876"},
            )
            assert login.status_code == 303
            assert "httponly" in login.headers["set-cookie"].lower()
            assert (await client.get("/_toolframe/v1/status")).status_code == 200
            assert (await client.get("/llms.txt")).text == "provider response"
            assert (
                await client.get(
                    "/plugins", headers={"Origin": "http://192.168.1.10:9876"}
                )
            ).text == "provider response"
    finally:
        await host.close()


def test_lan_websocket_keeps_auth_and_same_origin_validation(tmp_path, monkeypatch):
    ConfigFile(tmp_path).initialize(host="0.0.0.0")
    host = Host(tmp_path)
    connections = []

    class Upstream:
        subprotocol = "streamlit"

        def __init__(self):
            self.messages = asyncio.Queue()

        async def send(self, message):
            await self.messages.put(message)

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.messages.get()

    @asynccontextmanager
    async def connect(url, **kwargs):
        connections.append((url, kwargs))
        yield Upstream()

    monkeypatch.setattr(api, "connect", connect)
    monkeypatch.setattr(
        api,
        "provider_for",
        lambda host, path: (
            "dashboard",
            SimpleNamespace(name="dashboard"),
            "http://127.0.0.1:12345",
        ),
    )
    headers = {"Host": "192.168.1.10:9876", "Origin": "http://192.168.1.10:9876"}
    try:
        with TestClient(api.create_app(host, manage_lifespan=False)) as client:
            for invalid in (
                {
                    **headers,
                    "Cookie": f"{api.COOKIE}={host.token}",
                    "Origin": "http://attacker.example",
                },
                {
                    **headers,
                    "Cookie": f"{api.COOKIE}={host.token}",
                    "Host": "attacker.example",
                },
            ):
                with (
                    pytest.raises(WebSocketDisconnect) as error,
                    client.websocket_connect("/_stcore/stream", headers=invalid),
                ):
                    pytest.fail("Untrusted WebSocket was accepted")
                assert error.value.code == 1008
            with client.websocket_connect(
                "/_stcore/stream",
                subprotocols=["streamlit"],
                headers={**headers, "Cookie": f"{api.COOKIE}={host.token}"},
            ) as websocket:
                websocket.send_text("hello")
                assert websocket.receive_text() == "hello"
        assert len(connections) == 1
        assert connections[0][1]["origin"] == "http://127.0.0.1:12345"
    finally:
        host.state.close()
