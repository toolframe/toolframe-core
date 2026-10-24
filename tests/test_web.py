import time
from types import SimpleNamespace

import pytest
from toolframe_sdk import (
    AuthorizationSpec,
    MethodSpec,
    PluginDefinition,
    PluginError,
    Principal,
    Resource,
    ScopeSpec,
    ServiceSpec,
)
from toolframe_sdk.web import WebAppCatalog, WebRouteSpec

from toolframe_core.access import Identity
from toolframe_core.config import (
    Application,
    Composition,
    GatewaySettings,
    PluginRequest,
    PresentationMount,
)
from toolframe_core.gateway import compile_gateway, return_path, validate_web_catalogs
from toolframe_core.graph import Graph
from toolframe_core.host import Host
from toolframe_core.web_access import WebAccess


def composition(**mounts):
    return Composition(
        schema_version=2,
        application=Application(id="test"),
        plugins={"example": PluginRequest(source="pypi:example")},
        gateway=GatewaySettings(
            mounts={
                target: PresentationMount(prefix=prefix)
                for target, prefix in mounts.items()
            }
        ),
    )


def graph():
    definition = PluginDefinition(
        id="example",
        factory="example:Plugin",
        authorization=AuthorizationSpec(
            scopes={
                "example.read": ScopeSpec(title="Read"),
                "example.write": ScopeSpec(title="Write"),
            }
        ),
        services={
            "example.notes": ServiceSpec(
                methods={
                    "list": MethodSpec(
                        audience="business",
                        required_scopes=("example.read",),
                        read_only=True,
                    ),
                    "write": MethodSpec(
                        audience="business", required_scopes=("example.write",)
                    ),
                    "internal": MethodSpec(audience="internal"),
                    "admin": MethodSpec(
                        audience="system", required_scopes=("system.plugins.manage",)
                    ),
                }
            )
        },
        resources=(
            Resource(
                id="main",
                kind="web.app",
                required_scopes=("example.read",),
                data={
                    "contract": 1,
                    "entrypoint": "example.web:app",
                    "package": "example",
                    "assets": "web/assets.json",
                },
            ),
        ),
    )
    provider = PluginDefinition(
        id="web",
        kind="provider",
        factory="web:Plugin",
        provider_kind="web",
        authorization=AuthorizationSpec(),
    )
    return Graph(
        {
            "example": {"definition": definition.model_dump()},
            "web": {"definition": provider.model_dump()},
        },
        {},
    )


def catalog(**changes):
    values = {
        "routes": (
            WebRouteSpec(id="index", path="/", methods=("GET",)),
            WebRouteSpec(
                id="save",
                path="/",
                methods=("POST",),
                required_scopes=("example.write",),
            ),
        ),
        "asset_digest": "a" * 64,
        "asset_paths": ("app.js",),
    }
    return WebAppCatalog(**{**values, **changes})


class Access:
    async def admit(self, identity, scopes=(), **kwargs):
        if not identity.session:
            raise PluginError("authentication_required", "Sign in")
        allowed = (
            ("example.read", "example.write")
            if identity.session == "write"
            else ("example.read",)
        )
        if not set(scopes) <= set(allowed):
            raise PluginError("forbidden", "Scope denied")
        return Principal(
            kind="password", credential_id=identity.session, scopes=allowed
        )

    async def presentation(self, identity, *, principal=None):
        return {"actor": "password", "allowed_scopes": ["example.read"]}


@pytest.fixture
def host():
    value = SimpleNamespace(
        composition=composition(**{"web.example.main": "/notes"}),
        graph=graph(),
        token="private host token",
        epoch="epoch",
        gated=set(),
        access=Access(),
        publication={"web": {"state": "available"}},
        invocations=[],
    )
    value.gateway = compile_gateway(value.composition, value.graph)
    value.runtimes = {
        "example": SimpleNamespace(
            name="example", generation="owner-v1", admitted=True
        ),
        "web": SimpleNamespace(
            name="web",
            generation="provider-v1",
            epoch="epoch",
            candidate={"definition": {"provider_kind": "web"}},
        ),
    }
    value.web_access = WebAccess(value)
    resource = value.graph.resources({"example": "owner-v1"})[0]
    value.web_access.publish({resource.key: catalog()})
    value.begin_root = lambda owner, principal: SimpleNamespace(
        owner=owner, principal=principal
    )

    async def invoke(root, owner, service, method, params):
        value.invocations.append((root.principal, owner, method))
        return {"ok": True}

    async def end_root(root):
        pass

    value.invoke, value.end_root = invoke, end_root
    return value


def request(host, method="GET", identity=None, path="/"):
    resource = host.graph.resources({"example": "owner-v1"})[0]
    runtime = host.runtimes["web"]
    reference = host.web_access.register(
        identity or Identity(session="write"),
        runtime,
        resource,
        method,
        path,
        host.gateway.mounts["web.example.main"],
        host.gateway,
    )
    return runtime, {
        "key": resource.key,
        "epoch": "epoch",
        "access_context": reference,
        "route": "index" if method == "GET" else "save",
    }


def test_gateway_roots_are_explicit_and_match_segments():
    gateway = compile_gateway(
        composition(**{"web.example.main": "/", "provider.dashboard": "/console"})
    )
    assert gateway.match("/console/notes").target == "provider.dashboard"
    assert gateway.match("/console-other").target == "web.example.main"
    assert gateway.match("/_toolframe/unknown") is None
    assert gateway.match("/.well-known/unknown") is None
    assert gateway.match("/mcp").target == "provider.mcp"
    assert gateway.landing_url == "/"
    reverse = compile_gateway(
        composition(**{"web.example.main": "/notes", "provider.dashboard": "/"})
    )
    assert reverse.match("/notes/item").target == "web.example.main"
    assert reverse.match("/other").target == "provider.dashboard"
    # Disabled owners remain selected; dispatch reports unavailable rather than reassigning root.
    assert not composition(**{"web.example.main": "/"}).plugins["example"].enabled
    assert gateway.match("/").target == "web.example.main"


@pytest.mark.parametrize(
    "mounts",
    [
        {"web.example.main": "/", "provider.dashboard": "/"},
        {"web.example.main": "/_toolframe/site"},
        {"web.example.main": "/", "provider.dashboard": "/_tf/stolen"},
        {"web.missing.main": "/"},
        {"provider.mcp": "/agents"},
        {"app.example": "/"},
    ],
)
def test_invalid_mounts_fail_before_activation(mounts):
    with pytest.raises(ValueError):
        composition(**mounts)


def test_declared_route_cannot_be_shadowed_by_a_nested_mount():
    app_graph = graph()
    gateway = compile_gateway(
        composition(**{"web.example.main": "/", "provider.dashboard": "/notes/fixed"})
    )
    resource = app_graph.resources()[0]
    descriptor = catalog(
        routes=(WebRouteSpec(id="page", path="/notes/{id}/detail", methods=("GET",)),)
    )
    with pytest.raises(PluginError, match="shadowed"):
        validate_web_catalogs(
            gateway, [resource], {resource.key: descriptor}, app_graph
        )


def test_return_paths_cannot_redirect_off_origin_or_into_auth_loops():
    gateway = compile_gateway(composition(**{"web.example.main": "/notes"}))
    for value in (
        "//attacker/x",
        "https://attacker/x",
        "/%2f%2fattacker",
        "/notes/../oauth",
        "/_toolframe/auth/login",
        "/notes\\evil",
        "/notes\r\nHeader: bad",
    ):
        assert return_path(value, gateway) == "/"
    assert (
        return_path("/notes/item?query=hello%20world", gateway)
        == "/notes/item?query=hello%20world"
    )


async def test_handler_and_service_scopes_are_both_checked(host):
    runtime, params = request(host, identity=Identity(session="read"))
    await Host.worker_call(host, runtime, "resource.admit", params)
    result = await Host.worker_call(
        host, runtime, "resource.call", {**params, "method": "example.notes@1/list"}
    )
    assert result == {"ok": True}
    assert host.invocations[0][0].credential_id == "read"
    for method in ("write", "internal", "admin"):
        with pytest.raises(PluginError, match="business|read-only|Internal"):
            await Host.worker_call(
                host,
                runtime,
                "resource.call",
                {**params, "method": "example.notes@1/" + method},
            )
    runtime, write = request(host, "POST", identity=Identity(session="read"))
    with pytest.raises(PluginError, match="Scope denied"):
        await Host.worker_call(host, runtime, "resource.admit", write)


async def test_unsafe_requests_need_current_revision_and_csrf(host):
    runtime, get = request(host)
    page = await Host.worker_call(host, runtime, "resource.admit", get)
    runtime, post = request(host, "POST")
    with pytest.raises(PluginError) as error:
        await Host.worker_call(
            host,
            runtime,
            "resource.admit",
            {**post, "csrf": page["csrf"], "revision": "old"},
        )
    assert error.value.code == "web_revision_stale"
    with pytest.raises(PluginError) as error:
        await Host.worker_call(
            host,
            runtime,
            "resource.admit",
            {**post, "csrf": "forged", "revision": page["revision"]},
        )
    assert error.value.code == "csrf_failed"
    await Host.worker_call(
        host,
        runtime,
        "resource.admit",
        {**post, "csrf": page["csrf"], "revision": page["revision"]},
    )
    await Host.worker_call(
        host, runtime, "resource.call", {**post, "method": "example.notes@1/write"}
    )
    assert host.invocations[-1][2] == "write"


async def test_references_are_bound_to_route_resource_epoch_and_lifetime(host):
    runtime, params = request(host)
    with pytest.raises(PluginError, match="not admitted"):
        await Host.worker_call(
            host, runtime, "resource.call", {**params, "method": "example.notes@1/list"}
        )
    for changes in (
        {"key": "example/web.app/other"},
        {"route": "save"},
        {"epoch": "previous"},
        {"access_context": "forged"},
    ):
        with pytest.raises(PluginError):
            await Host.worker_call(
                host, runtime, "resource.admit", {**params, **changes}
            )
    await Host.worker_call(host, runtime, "resource.admit", params)
    host.web_access.release(params["access_context"])
    with pytest.raises(PluginError, match="ended"):
        await Host.worker_call(
            host, runtime, "resource.call", {**params, "method": "example.notes@1/list"}
        )
    runtime, params = request(host)
    host.web_access.contexts[params["access_context"]].deadline = time.monotonic() - 1
    with pytest.raises(PluginError, match="expired"):
        await Host.worker_call(host, runtime, "resource.admit", params)


async def test_previous_assets_use_current_policy_and_expire(host):
    key = host.graph.resources()[0].key
    host.web_access.publish({key: catalog(asset_visibility="public")})
    host.web_access.publish(
        {key: catalog(asset_digest="b" * 64, asset_visibility="inherit")}
    )
    runtime, params = request(
        host, path="/_tf/assets/" + "a" * 64 + "/app.js", identity=Identity()
    )
    # request() defaults only when identity is None; an anonymous identity stays anonymous.
    with pytest.raises(PluginError, match="Sign in"):
        await Host.worker_call(
            host, runtime, "resource.admit", {**params, "asset": "a" * 64 + "/app.js"}
        )
    runtime, params = request(host, path="/_tf/assets/" + "a" * 64 + "/app.js")
    assert (
        await Host.worker_call(
            host, runtime, "resource.admit", {**params, "asset": "a" * 64 + "/app.js"}
        )
    )["visibility"] == "inherit"
    host.web_access.previous[key] = (catalog(), time.monotonic() - 1)
    with pytest.raises(PluginError, match="Unknown Web asset"):
        await Host.worker_call(
            host, runtime, "resource.admit", {**params, "asset": "a" * 64 + "/app.js"}
        )


def test_web_and_dashboard_have_independent_attachment_cohorts():
    before = graph()
    after = graph()
    assert before.affected(after, {"example"}) == {"example", "web"}
