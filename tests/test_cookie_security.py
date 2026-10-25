"""Cookie attributes at real ASGI boundaries; browser transport needs separate QA."""

# ruff: noqa: F811 -- shared pytest fixtures.
import time
from http.cookies import SimpleCookie
from types import SimpleNamespace

import httpx
import pytest
from test_auth import app_host, credential  # noqa: F401
from test_host import host  # noqa: F401

from toolframe_core.access import PASSWORD_COOKIE
from toolframe_core.api import COOKIE, create_app
from toolframe_core.config import Application, PluginRequest
from toolframe_core.oidc_access import BROWSER_COOKIE

CASES = [
    ("https://app.example", "http://app.example", True),
    ("HTTPS://app.example", "http://app.example", True),
    ("hTtPs://app.example", "http://app.example", True),
    ("HTTPS://app.example", "https://app.example", True),
    ("http://127.0.0.1:9876", "http://127.0.0.1:9876", False),
    ("HTTP://localhost:9876", "http://localhost:9876", False),
    ("HTTP://localhost:9876", "https://localhost:9876", True),
    ("", "https://127.0.0.1:9876", True),
]


def assert_cookie(response, name, *, secure, samesite):
    jar = SimpleCookie()
    for header in response.headers.get_list("set-cookie"):
        jar.load(header)
    cookie = jar[name]
    assert bool(cookie["secure"]) is secure
    assert cookie["httponly"]
    assert cookie["samesite"].lower() == samesite
    assert cookie["path"] == "/" and not cookie["domain"]
    return cookie


@pytest.mark.parametrize("public_url,base_url,secure", CASES)
async def test_password_and_operator_cookie_scheme(
    app_host, public_url, base_url, secure
):
    host = app_host
    await credential(host, scopes=["system.plugins.read"])
    host.composition.application = Application(
        id=host.composition.application.id, public_url=public_url
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            create_app(host, manage_lifespan=False), client=("192.0.2.10", 5000)
        ),
        base_url=base_url,
    ) as browser:
        login = await browser.post(
            "/_toolframe/auth/login", json={"password": "test-password-read"}
        )
        assert login.status_code == 303
        cookie = assert_cookie(login, PASSWORD_COOKIE, secure=secure, samesite="strict")
        assert int(cookie["max-age"]) > 0
        # Use an explicit Cookie header when simulating the cleartext proxy hop.
        status = await browser.get(
            "/_toolframe/v1/status",
            headers={"Cookie": f"{PASSWORD_COOKIE}={cookie.value}"},
        )
        assert status.status_code == 200
        for extra in (
            {"Origin": "https://attacker.example"},
            {"Host": "attacker.example"},
        ):
            rejected = await browser.post(
                "/_toolframe/auth/login",
                json={"password": "test-password-read"},
                headers=extra,
            )
            assert rejected.status_code == 403
            assert "set-cookie" not in rejected.headers
        operator = await browser.post("/_toolframe/session", data={"token": host.token})
        assert operator.status_code == 303
        assert_cookie(operator, COOKIE, secure=secure, samesite="strict")


@pytest.mark.parametrize("public_url,base_url,secure", CASES[:-1])
async def test_oidc_browser_and_user_cookie_scheme(
    host, monkeypatch, public_url, base_url, secure
):
    host.composition.application = Application(
        id=host.composition.application.id, public_url=public_url
    )
    host.composition.access.oidc_provider = "oidc"
    host.composition.plugins["oidc"] = PluginRequest(
        source="pypi:fixture", config={"public_url": public_url}
    )
    host.graph.definitions["oidc"] = SimpleNamespace(provider_kind="oidc-auth")
    monkeypatch.setattr(host, "healthy_runtime", lambda name: name == "oidc")
    calls = []

    async def call_service(name, service, method, params, **kwargs):
        # The provider boundary is synthetic; Request/Response and routing are real.
        assert (name, service, method) == ("oidc", "toolframe.oidc", "http")
        calls.append(params)
        return {
            "body": "",
            "status": 303,
            "headers": {"Location": "/"},
            **(
                {"browser": "dummy-transaction"}
                if "/login/" in params["path"]
                else {
                    "session": {
                        "token": "dummy-user-session",
                        "expires_at": time.time() + 600,
                    }
                }
            ),
        }

    monkeypatch.setattr(host, "call_service", call_service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(create_app(host, manage_lifespan=False)),
        base_url=base_url,
    ) as browser:
        login = await browser.get("/_toolframe/oidc/login/fixture")
        assert login.status_code == 303
        assert_cookie(login, BROWSER_COOKIE, secure=secure, samesite="lax")
        callback = await browser.get(
            "/_toolframe/oidc/callback/fixture?state=dummy&code=dummy"
        )
        assert callback.status_code == 303
        cookie = assert_cookie(callback, PASSWORD_COOKIE, secure=secure, samesite="lax")
        assert 1 <= int(cookie["max-age"]) <= 600
        alternate = await browser.get(
            "/_toolframe/oidc/login/fixture", headers={"Host": "127.0.0.2:9876"}
        )
        assert alternate.status_code == 303
        assert (
            alternate.headers["location"]
            == public_url + "/_toolframe/oidc/login/fixture"
        )
        assert "set-cookie" not in alternate.headers
        assert len(calls) == 2
