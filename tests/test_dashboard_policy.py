"""Core protects Dashboard documents without replacing upstream CSP policies."""

# ruff: noqa: F811 -- shared pytest fixture.
from types import SimpleNamespace

import httpx
import pytest
from fastapi.responses import Response
from test_host import host  # noqa: F401
from toolframe_sdk import PluginError

from toolframe_core import api, web_gateway
from toolframe_core.config import GatewaySettings, PluginRequest, PresentationMount
from toolframe_core.gateway import compile_gateway


@pytest.mark.parametrize("profile", [1, 2])
@pytest.mark.parametrize("upstream_policy", [False, True])
async def test_dashboard_frame_policy_preserves_upstream_and_top_level(
    host, monkeypatch, profile, upstream_policy
):
    host.graph.providers["dashboard"] = "fixture-dashboard"
    host.composition.schema_version = profile
    base = "/" if profile == 1 else "/console/"
    if profile == 2:
        host.composition.plugins["example"] = PluginRequest(source="pypi:fixture")
        host.composition.gateway = GatewaySettings(
            mounts={
                "provider.dashboard": PresentationMount(prefix="/console"),
                "web.example.main": PresentationMount(prefix="/notes"),
            }
        )
        host.gateway = compile_gateway(host.composition)
    original_client = httpx.AsyncClient
    existing = [
        "default-src 'self'; script-src 'self'",
        "frame-ancestors 'self'; object-src 'none'",
    ]

    def upstream(request):
        headers = [("Content-Type", "text/html"), ("X-Upstream", "retained")]
        if upstream_policy:
            headers += [("Content-Security-Policy", value) for value in existing]
            headers += [("X-Frame-Options", "SAMEORIGIN")]
        return httpx.Response(
            200, headers=headers, stream=httpx.ByteStream(b"Dashboard")
        )

    def provider(host, path):
        mount = host.gateway.match("/" + path)
        kind = "web" if mount and mount.target.startswith("web.") else "dashboard"
        return kind, SimpleNamespace(name="fixture-dashboard"), "http://127.0.0.1:12345"

    monkeypatch.setattr(api, "provider_for", provider)
    monkeypatch.setattr(
        api.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(upstream)),
    )

    async def forward(*args):
        return Response(
            "Web",
            headers={
                "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'"
            },
        )

    monkeypatch.setattr(web_gateway, "forward", forward)
    async with original_client(
        transport=httpx.ASGITransport(api.create_app(host, manage_lifespan=False)),
        base_url="http://127.0.0.1:9876",
    ) as browser:
        for path in (
            base,
            base + "plugins",
            base + "static/app.js",
            base + "_stcore/health",
            *(["/api/unknown", "/oauth/unknown"] if profile == 1 else []),
        ):
            response = await browser.get(
                path,
                headers={
                    "Sec-Fetch-Site": "same-origin",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Dest": "document",
                },
            )
            assert response.status_code == 200 and response.text == "Dashboard"
            assert response.headers["x-upstream"] == "retained"
            assert response.headers["x-frame-options"] == "DENY"
            assert response.headers.get_list("content-security-policy") == [
                *(existing if upstream_policy else []),
                "frame-ancestors 'none'",
            ]
        if profile == 2:
            web = await browser.get("/notes/")
            assert web.status_code == 200 and web.text == "Web"
            assert web.headers.get_list("content-security-policy") == [
                "default-src 'none'; frame-ancestors 'none'"
            ]
            assert "x-frame-options" not in web.headers
        for path in (
            "/_toolframe/auth/login",
            "/_toolframe/session",
            "/_toolframe/v1/status",
        ):
            response = await browser.get(path)
            assert response.status_code == 200 or (
                path.endswith("login") and response.status_code == 503
            )
            assert response.headers["x-frame-options"] == "DENY"
            assert "frame-ancestors 'none'" in response.headers.get_list(
                "content-security-policy"
            )

        async def denied(*args, **kwargs):
            raise PluginError("authentication_required", "Sign in")

        monkeypatch.setattr(host.access, "admit", denied)
        response = await browser.get(base + "plugins")
        assert response.status_code == 303
        assert response.headers["location"] == "/_toolframe/auth/login"
        assert response.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers.get_list(
            "content-security-policy"
        )

        def unavailable(*args):
            raise PluginError("presentation_unavailable", "Unavailable")

        monkeypatch.setattr(api, "provider_for", unavailable)
        response = await browser.get(base)
        assert response.status_code == 503
        assert response.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers.get_list(
            "content-security-policy"
        )
        response = await browser.get(
            base, headers={"Origin": "https://attacker.example"}
        )
        assert response.status_code == 403
        assert response.headers["x-frame-options"] == "DENY"
