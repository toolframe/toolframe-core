# ruff: noqa: F811 -- shared pytest fixture imported from test_web.
from types import SimpleNamespace
from urllib.parse import unquote

import httpx
import pytest
from starlette.requests import Request
from test_web import host, request  # noqa: F401 - shared Host fixture
from toolframe_sdk import PluginError, Principal
from toolframe_sdk.web import WebRouteSpec

from toolframe_core.access import Access, Identity
from toolframe_core.graph import Graph
from toolframe_core.web_gateway import forward


async def test_authorization_phase_cannot_invoke_and_rechecks_before_proofs(host):
    runtime, params = request(host, method="POST")
    entry = host.web_access.contexts[params["access_context"]]
    result = await host.web_access.admit(runtime, {**params, "phase": "authorize"})
    assert result == {"authorized": True} and not entry.admitted
    with pytest.raises(PluginError, match="not admitted"):
        await host.web_access.resolve(runtime, params, ("example.write",))
    with pytest.raises(PluginError) as stale:
        await host.web_access.admit(runtime, params)
    assert stale.value.code == "web_revision_stale" and not entry.admitted
    proof = {**params, "csrf": host.web_access.csrf(entry), "revision": entry.revision}
    entry.identity = Identity(session="read")
    with pytest.raises(PluginError) as revoked:
        await host.web_access.admit(runtime, proof)
    assert revoked.value.code == "forbidden" and not entry.admitted
    entry.identity = Identity(session="write")
    await host.web_access.admit(runtime, proof)
    assert entry.admitted
    entry.identity = Identity(session="read")
    with pytest.raises(PluginError):
        await host.web_access.resolve(runtime, params, ("example.write",))


@pytest.mark.parametrize("phase", [None, "", "skip", [], True])
async def test_unknown_admission_phase_fails_closed(host, phase):
    runtime, params = request(host)
    with pytest.raises(PluginError) as error:
        await host.web_access.admit(runtime, {**params, "phase": phase})
    assert error.value.code == "invalid_request"
    assert not host.web_access.contexts[params["access_context"]].admitted


@pytest.mark.parametrize("mode", ["public", "protected"])
@pytest.mark.parametrize("resource_policy", ["inherit", "authenticated"])
@pytest.mark.parametrize("role", ["anonymous", "read", "write"])
async def test_presentation_route_and_service_use_same_principal(
    host, mode, resource_policy, role
):
    definitions = {
        name: {"definition": definition.model_dump()}
        for name, definition in host.graph.definitions.items()
    }
    definitions["example"]["definition"]["resources"][0]["data"]["access"] = (
        resource_policy
    )
    host.graph = Graph(definitions, {})
    host.access = Access(host)

    async def configured_mode():
        return {"mode": mode, "configured": mode, "reason": "configured_" + mode}

    async def auth(method, params, **kwargs):
        scopes = (
            ("example.read", "example.write") if role == "write" else ("example.read",)
        )
        return {
            "principal": Principal(
                kind="password", credential_id=role, scopes=scopes
            ).model_dump()
        }

    host.access.mode = configured_mode
    host.access.auth = auth
    identity = Identity(session="" if role == "anonymous" else role)
    runtime, params = request(host, identity=identity)
    may_read = role != "anonymous" or (
        mode == "public" and resource_policy == "inherit"
    )
    may_write = role == "write" or (mode == "public" and resource_policy == "inherit")
    if not may_read:
        with pytest.raises(PluginError) as error:
            await host.web_access.admit(runtime, params)
        assert error.value.code == "authentication_required"
        assert "web.example.main" not in await host.web_access.links(identity)
        return
    admission = await host.web_access.admit(runtime, params)
    assert ("example.write" in admission["presentation"]["allowed_scopes"]) == may_write
    assert admission["links"]["web.example.main"] == "/notes/"
    await host.web_access.resolve(runtime, params, ("example.read",))
    runtime, write = request(host, method="POST", identity=identity)
    entry = host.web_access.contexts[write["access_context"]]
    write.update(csrf=host.web_access.csrf(entry), revision=entry.revision)
    if may_write:
        await host.web_access.admit(runtime, write)
        await host.web_access.resolve(runtime, write, ("example.write",))
    else:
        with pytest.raises(PluginError) as error:
            await host.web_access.admit(runtime, write)
        assert error.value.code == "forbidden"


@pytest.mark.parametrize("identifier", ["a?b", "a#b", "香港", "two words", "a?b#雪"])
async def test_sdk_gateway_provider_path_and_query_roundtrip(
    host, monkeypatch, identifier
):
    host.gateway_sessions = {}
    host.healthy_runtime = lambda name: True
    route = WebRouteSpec(id="detail", path="/item/{id}", methods=("GET",))
    raw_path = "/notes" + route.url({"id": identifier})
    query = b"filter=a%3Fb%23c&tag=%E9%9B%AA&tag=two+words"
    scope = {
        "type": "http",
        "method": "GET",
        "scheme": "http",
        "path": unquote(raw_path),
        "raw_path": raw_path.encode(),
        "query_string": query,
        "headers": [(b"host", b"127.0.0.1")],
        "state": {"identity": Identity(session="read")},
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    observed = []

    class ResponseStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"ok"

    def provider(upstream):
        observed.append((route.match(upstream.url.path), upstream.url.query))
        return httpx.Response(200, stream=ResponseStream())

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(provider), **kwargs),
    )
    response = await forward(
        host,
        Request(scope, receive),
        host.gateway.mounts["web.example.main"],
        host.runtimes["web"],
        "http://127.0.0.1:9879",
    )
    assert response.status_code == 200
    assert observed == [({"id": identifier}, query)]
    assert not host.web_access.contexts


@pytest.mark.parametrize(
    "path",
    ["/notes/%2f", "/notes/%5c", "/notes/%2e", "/notes/%00", "/notes/%25", "/notes//x"],
)
async def test_gateway_still_rejects_noncanonical_paths(host, path):
    scope = {
        "type": "http",
        "method": "GET",
        "scheme": "http",
        "path": unquote(path),
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
    }
    result = await forward(
        host,
        Request(scope),
        SimpleNamespace(prefix="/notes"),
        None,
        "http://127.0.0.1:9879",
    )
    assert result.status_code == 400
