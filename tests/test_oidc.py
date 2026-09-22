"""Public OIDC gateway and optional verifier isolation with real plugin workers."""

# ruff: noqa: F811 -- shared pytest fixtures.
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from test_auth import ROOT, app_host, client, credential, operate  # noqa: F401
from test_mcp_auth import key, mcp_host, policy  # noqa: F401
from toolframe_sdk import PluginError

from toolframe_core import api
from toolframe_core.access import Access
from toolframe_core.api import create_app
from toolframe_core.config import Application
from toolframe_core.network import trusted
from toolframe_core.oidc_access import cross_site_route
from toolframe_core.redaction import redact


def test_public_origin_does_not_inherit_proxy_loopback_authority():
    host = SimpleNamespace(
        composition=SimpleNamespace(
            application=Application(id="test", public_url="https://app.example"),
            access=SimpleNamespace(local_control=True),
        ),
        token="operator-token",
    )
    access = Access(host)
    connection = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        headers={"host": "app.example"},
        cookies={},
    )
    assert not access.identity(connection).local
    connection.headers = {"host": "127.0.0.1:9876"}
    assert access.identity(connection).local
    connection.headers["x-forwarded-for"] = "192.0.2.1"
    assert not access.identity(connection).local
    headers = {
        "host": "app.example",
        "origin": "https://agent.example",
        "sec-fetch-site": "cross-site",
    }
    assert not trusted(headers, "0.0.0.0", public_url="https://app.example")
    assert trusted(
        headers, "0.0.0.0", public_url="https://app.example", cross_site=True
    )
    assert cross_site_route("/oauth/token", "POST")
    assert not cross_site_route("/oauth/consent", "POST")
    assert not cross_site_route("/_toolframe/v1/status", "GET")
    for prefix in ("tf_oauth_", "tf_refresh_", "tf_user_"):
        assert redact(prefix + "a" * 43, set()) == "[REDACTED]"
    assert "private" not in redact(
        "https://app.example/cb?code=private&state=private", set()
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.example",
        "https://app.example/path",
        "https://u:p@app.example",
        "https://app.example/#fragment",
    ],
)
def test_invalid_public_origin(url):
    with pytest.raises(ValueError):
        Application(id="test", public_url=url)


async def test_dashboard_return_keeps_auth_and_cross_site_api_protection(
    app_host, monkeypatch
):
    host = app_host
    await credential(host)
    host.composition.application.public_url = "https://app.example"
    host.graph.providers["dashboard"] = "fixture-dashboard"
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        api,
        "provider_for",
        lambda host, path: (
            "dashboard",
            SimpleNamespace(name="fixture-dashboard"),
            "http://127.0.0.1:12345",
        ),
    )
    monkeypatch.setattr(
        api.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, stream=httpx.ByteStream(b"Dashboard")
                )
            )
        ),
    )
    navigation = {
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
    }
    async with original_client(
        transport=httpx.ASGITransport(
            create_app(host, manage_lifespan=False), client=("127.0.0.1", 5000)
        ),
        base_url="https://app.example",
    ) as browser:
        # Returning from an IdP may navigate, but still requires a local session.
        anonymous = await browser.get("/", headers=navigation)
        assert anonymous.status_code == 303
        assert anonymous.headers["location"] == "/_toolframe/auth/login"
        assert (
            await browser.get(anonymous.headers["location"], headers=navigation)
        ).status_code == 200
        login = await browser.post(
            "/_toolframe/auth/login", json={"password": "test-password-read"}
        )
        assert login.status_code == 303
        landing = await browser.get("/", headers=navigation)
        assert landing.status_code == 200 and landing.text == "Dashboard"
        for method, path, extra in (
            ("POST", "/", {}),
            ("POST", "/_toolframe/auth/login", {}),
            ("POST", "/oauth/consent", {}),
            ("GET", "/_toolframe/v1/status", {}),
            ("GET", "/_toolframe/session", {}),
            ("GET", "/mcp", {}),
            ("GET", "/", {"Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}),
            ("GET", "/", {"Sec-Fetch-Dest": "iframe"}),
            ("GET", "/", {"Origin": "https://identity.example"}),
            ("GET", "/", {"Host": "127.0.0.1:9876"}),
        ):
            rejected = await browser.request(
                method, path, headers={**navigation, **extra}
            )
            assert rejected.status_code == 403, (method, path, extra)
            assert rejected.json()["error"]["code"] == "untrusted_origin"
        # The proxy's loopback peer must not promote the session to operator.
        assert (await browser.get("/_toolframe/v1/status")).status_code == 403
        host.composition.application.public_url = "http://127.0.0.1:9876"
        assert (
            await browser.get("/", headers={**navigation, "Host": "127.0.0.1:9876"})
        ).status_code == 403


async def test_discovery_gateway_and_optional_oidc_failure_preserve_keys(
    mcp_host, tmp_path
):
    host = mcp_host
    secret = tmp_path / "upstream-secret"
    secret.write_text("test-only")
    secret.chmod(0o600)
    await operate(
        host,
        "add",
        source=str(ROOT / "toolframe-oidc-auth"),
        enable=True,
        config={
            "public_url": "https://app.example",
            "scopes": ["entry-register.read"],
            "dynamic_registration": True,
            "providers": {
                "fixture": {
                    "issuer": "https://identity.example",
                    "server_metadata_url": "https://identity.example/.well-known/openid-configuration",
                    "client_id": "toolframe",
                    "client_secret_file": str(secret),
                }
            },
        },
    )
    await policy(host, "protected")

    def configure(document):
        document["application"]["public_url"] = "https://app.example"
        document.setdefault("access", {}).update(
            {
                "mode": "protected",
                "oidc_provider": "oidc-auth",
                "failure_mode": "closed",
            }
        )

    host.composition, host.revision = host.config.edit(host.config.revision, configure)
    existing = await key(host)
    async with client(host) as alternate:
        redirect = await alternate.get("/_toolframe/oidc/login/fixture")
        assert redirect.status_code == 303
        assert (
            redirect.headers["location"]
            == "https://app.example/_toolframe/oidc/login/fixture"
        )
        assert not redirect.cookies
    transport = httpx.ASGITransport(
        create_app(host, manage_lifespan=False), client=("127.0.0.1", 5000)
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="https://app.example"
    ) as browser:
        assert (await browser.get("/_toolframe/v1/status")).status_code == 401
        prm = await browser.get(
            "/.well-known/oauth-protected-resource/mcp",
            headers={"Origin": "https://agent.example"},
        )
        assert (
            prm.status_code == 200 and prm.headers["access-control-allow-origin"] == "*"
        )
        assert prm.json()["authorization_servers"] == ["https://app.example/oauth"]
        metadata = (
            await browser.get("/.well-known/oauth-authorization-server/oauth")
        ).json()
        assert metadata["code_challenge_methods_supported"] == ["S256"]
        assert (
            (await browser.post("/mcp", json={})).headers["www-authenticate"]
            == 'Bearer resource_metadata="https://app.example/.well-known/oauth-protected-resource/mcp"'
        )
        invalid = await browser.get(
            "/_toolframe/oidc/callback/fixture?state=invalid&code=invalid",
            headers={"sec-fetch-site": "cross-site"},
        )
        assert invalid.status_code == 422 and host.access.mode()["mode"] == "protected"
        assert (
            await browser.post(
                "/oauth/register",
                content="[]",
                headers={"content-type": "application/json"},
            )
        ).status_code == 400
    # Optional OIDC may remain installed but unhealthy; key admission and health
    # probes still work. Only OAuth calls depend on that optional worker.
    runtime = host.runtimes["oidc-auth"]
    runtime.health = {"runtime": "failed"}
    host.gated.add("oidc-auth")
    result = await host.mcp_access.auth("verify", {"token": existing["token"]})
    assert result["principal"]["kind"] == "mcp_key"
    assert (await host.probe(host.runtimes["mcp-auth"]))["runtime"] == "ready"
    with pytest.raises(PluginError):
        await host.mcp_access.auth("verify", {"token": "tf_oauth_" + "a" * 43})
    host.access.mark_failed("auth")
    assert host.access.mode()["mode"] == "waiting"
    await asyncio.sleep(0)
