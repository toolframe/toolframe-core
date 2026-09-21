import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import tomlkit
from toolframe_sdk import PluginError

from toolframe_core import api
from toolframe_core.api import create_app
from toolframe_core.config import ConfigFile, atomic_write
from toolframe_core.host import Host


def fixture_source(
    directory, name, *, dependency=None, version="old", delay=0, broken=False
):
    source = directory / name
    package = source / "fixture"
    package.mkdir(parents=True, exist_ok=True)
    (source / "pyproject.toml").write_text(f"""[build-system]
requires=["hatchling>=1.27,<2"]
build-backend="hatchling.build"
[project]
name="fixture-{name}"
version="0.1.0"
dependencies=["toolframe-sdk==0.2.0"]
[project.entry-points."toolframe.plugins"]
{name}="fixture:plugin"
[tool.hatch.build.targets.wheel]
packages=["fixture"]
""")
    requires = (
        f"{{'backend': Requirement(service='{dependency}.service')}}"
        if dependency
        else "{}"
    )
    response = (
        "return await self.context.call('backend', 'echo', params)"
        if dependency
        else f"return {{'version': '{version}'}}"
    )
    (package / "__init__.py").write_text(f"""import asyncio
from toolframe_sdk import AuthorizationSpec, ScopeSpec, Plugin, PluginDefinition, ServiceSpec, MethodSpec, Requirement
plugin=PluginDefinition(authorization=AuthorizationSpec(scopes={{"{name}.use":ScopeSpec(title="Use fixture")}}), id='{name}',factory='fixture:Example', requires={requires},
services={{'{name}.service': ServiceSpec(methods={{'echo':MethodSpec(audience="business", required_scopes=("{name}.use",), read_only=True)}})}})
class Example(Plugin):
    async def activate(self):
        {"raise RuntimeError('activation exploded')" if broken else "pass"}
    async def invoke(self, service, method, params):
        await asyncio.sleep(params.get('delay', {delay}))
        {response}
""")
    return source


async def operate(host, action, **params):
    operation = host.submit(action, params)
    return await host.wait_operation(operation["id"])


@pytest.fixture
async def host(tmp_path):
    home = tmp_path / "application"
    config = ConfigFile(home)
    composition = config.initialize()
    workspace = Path(__file__).resolve().parents[2]
    composition.packages.find_links = [str(workspace / "toolframe-sdk/dist")]
    composition.runtime.health_interval = 0.2
    atomic_write(config.path, tomlkit.dumps(composition.model_dump()))
    host = Host(home)
    await host.start()
    try:
        yield host
    finally:
        await host.close()


async def test_empty_core_and_disabled_add(host, tmp_path):
    assert host.status()["core"] == "ready"
    assert host.status()["plugins"] == []
    assert host.status()["dependency_graph"]["nodes"] == []
    source = fixture_source(tmp_path, "alpha")
    added = await operate(host, "add", id="alpha", source=str(source))
    assert added["status"] == "succeeded", added
    assert not host.runtimes
    enabled = await operate(host, "enable", id="alpha")
    assert enabled["status"] == "succeeded", enabled
    assert await host.call_service("alpha", "alpha.service", "echo", {}) == {
        "version": "old"
    }
    before = host.state.operations()
    report = host.status()["dependency_graph"]
    assert report["nodes"][0]["healthy"]
    assert report["impacts"]["alpha"]["reload"]["affected"] == ["alpha"]
    assert host.state.operations() == before
    removed = await operate(host, "remove", id="alpha")
    assert removed["status"] == "succeeded", removed
    assert not host.runtimes
    report = host.status()["dependency_graph"]
    assert report["nodes"][0]["state"] == "removed"
    assert not report["impacts"]


async def test_reload_replaces_required_cohort_but_keeps_unrelated_and_drains_nested_call(
    host, tmp_path
):
    for name, dependency in (
        ("alpha", None),
        ("consumer", "alpha"),
        ("unrelated", None),
    ):
        source = fixture_source(tmp_path, name, dependency=dependency)
        result = await operate(host, "add", id=name, source=str(source), enable=True)
        assert result["status"] == "succeeded", result
    old = {name: runtime.generation for name, runtime in host.runtimes.items()}
    call = asyncio.create_task(
        host.call_service("consumer", "consumer.service", "echo", {"delay": 0.3})
    )
    await asyncio.sleep(0.05)
    fixture_source(tmp_path, "alpha", version="new")
    replacement = asyncio.create_task(operate(host, "reload", id="alpha"))
    assert await call == {"version": "old"}
    result = await replacement
    assert result["status"] == "succeeded", result
    assert host.runtimes["alpha"].generation != old["alpha"]
    assert host.runtimes["consumer"].generation != old["consumer"]
    assert host.runtimes["unrelated"].generation == old["unrelated"]
    assert await host.call_service("consumer", "consumer.service", "echo", {}) == {
        "version": "new"
    }


async def test_activation_failure_recovers_old_snapshot_once(host, tmp_path):
    source = fixture_source(tmp_path, "alpha")
    assert (await operate(host, "add", id="alpha", source=str(source), enable=True))[
        "status"
    ] == "succeeded"
    old = host.runtimes["alpha"].generation
    fixture_source(tmp_path, "alpha", broken=True)
    result = await operate(host, "reload", id="alpha")
    assert result["status"] == "failed_recovered", result
    assert host.runtimes["alpha"].generation != old
    assert await host.call_service("alpha", "alpha.service", "echo", {}) == {
        "version": "old"
    }
    assert (
        host.state.plugin("alpha")["last_candidate"]
        != host.runtimes["alpha"].candidate["id"]
    )


async def test_drain_timeout_preserves_generation_and_reopens_admission(host, tmp_path):
    source = fixture_source(tmp_path, "alpha")
    assert (await operate(host, "add", id="alpha", source=str(source), enable=True))[
        "status"
    ] == "succeeded"
    old = host.runtimes["alpha"].generation
    host.composition.runtime.drain_timeout = 0.05
    atomic_write(host.config.path, tomlkit.dumps(host.composition.model_dump()))
    call = asyncio.create_task(
        host.call_service("alpha", "alpha.service", "echo", {"delay": 5})
    )
    await asyncio.sleep(0.01)
    result = await operate(host, "reload", id="alpha")
    assert (
        result["status"] == "failed" and result["error"]["code"] == "drain_timeout"
    ), result
    assert host.runtimes["alpha"].generation == old
    assert await host.call_service("alpha", "alpha.service", "echo", {}) == {
        "version": "old"
    }
    assert await call == {"version": "old"}


async def test_missing_dependency_is_visible_and_recovers_when_provider_added(
    host, tmp_path
):
    source = fixture_source(tmp_path, "consumer", dependency="alpha")
    result = await operate(host, "add", id="consumer", source=str(source), enable=True)
    assert result["status"] == "succeeded", result
    assert host.status()["plugins"][0]["blocked_by"] == ["alpha.service"]
    report = host.status()["dependency_graph"]
    assert report["edges"][0]["state"] == "missing"
    source = fixture_source(tmp_path, "alpha")
    result = await operate(host, "add", id="alpha", source=str(source), enable=True)
    assert result["status"] == "succeeded", result
    assert await host.call_service("consumer", "consumer.service", "echo", {}) == {
        "version": "old"
    }
    report = host.status()["dependency_graph"]
    assert report["edges"][0]["target"] == "alpha"
    assert report["edges"][0]["state"] == "resolved"


async def test_forced_stop_is_fenced_until_explicit_enable(host, tmp_path):
    source = fixture_source(tmp_path, "alpha")
    assert (await operate(host, "add", id="alpha", source=str(source), enable=True))[
        "status"
    ] == "succeeded"
    assert (await operate(host, "stop", id="alpha", force=True))[
        "status"
    ] == "succeeded"
    assert host.state.plugin("alpha")["stopped"]
    with pytest.raises(PluginError):
        await host.call_service("alpha", "alpha.service", "echo", {})
    result = await operate(host, "enable", id="alpha")
    assert result["status"] == "succeeded", result
    assert await host.call_service("alpha", "alpha.service", "echo", {}) == {
        "version": "old"
    }


async def test_management_auth_origin_and_empty_gateway(host):
    app = create_app(host, manage_lifespan=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app, client=("192.168.1.20", 54321)),
        base_url="http://127.0.0.1:9876",
    ) as client:
        assert (await client.get("/healthz")).json()["core"] == "ready"
        assert (await client.get("/")).json()["dashboard"] == "not_installed"
        assert (await client.get("/_toolframe/v1/status")).status_code == 401
        headers = {"Authorization": f"Bearer {host.token}"}
        assert (
            await client.get("/_toolframe/v1/status", headers=headers)
        ).status_code == 200
        assert (
            await client.get(
                "/_toolframe/v1/status",
                headers={**headers, "Origin": "https://attacker.example"},
            )
        ).status_code == 403
        assert (
            await client.get("/healthz", headers={"Host": "attacker.example"})
        ).status_code == 403
        login = await client.post("/_toolframe/session", content=f"token={host.token}")
        assert (
            login.status_code == 303
            and "httponly" in login.headers["set-cookie"].lower()
        )
        assert (await client.get("/_toolframe/v1/status")).status_code == 200
        response = await client.post(
            "/_toolframe/v1/operations",
            headers={**headers, "Idempotency-Key": "apply-once"},
            json={"action": "apply"},
        )
        identifier = response.json()["id"]
        assert response.status_code == 202
        await host.wait_operation(identifier)
        repeated = await client.post(
            "/_toolframe/v1/operations",
            headers={**headers, "Idempotency-Key": "apply-once"},
            json={"action": "apply"},
        )
        assert repeated.json()["id"] == identifier


async def test_real_llms_provider_http_through_gateway(host):
    workspace = Path(__file__).resolve().parents[2]
    result = await operate(
        host,
        "add",
        id="llms-txt",
        source=str(workspace / "toolframe-llms-txt"),
        enable=True,
    )
    assert result["status"] == "succeeded", result
    app = create_app(host, manage_lifespan=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9876"
    ) as client:
        response = await client.get("/llms.txt")
        assert response.status_code == 200, response.text
        assert response.text == ""
        assert (await client.get("/llms/unknown/doc.md")).status_code == 404


@pytest.mark.parametrize("partial_body", [False, True])
async def test_gateway_closes_upstream_on_upload_disconnect(
    host, monkeypatch, partial_body
):
    async def provider(request):
        await request.aread()
        pytest.fail("An incomplete request must not finish reaching the provider")

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    monkeypatch.setattr(api.httpx, "AsyncClient", lambda **kwargs: upstream)
    monkeypatch.setattr(
        api,
        "provider_for",
        lambda host, path: (
            "mcp",
            SimpleNamespace(name="mcp"),
            "http://127.0.0.1:12345",
        ),
    )
    messages = iter(
        [{"type": "http.request", "body": b'{"jsonrpc":', "more_body": True}]
        if partial_body
        else []
    )
    responses = []

    async def receive():
        return next(messages, {"type": "http.disconnect"})

    async def send(message):
        responses.append(message)

    await create_app(host, manage_lifespan=False)(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/mcp",
            "query_string": b"",
            "headers": [(b"host", b"127.0.0.1:9876")],
        },
        receive,
        send,
    )
    assert upstream.is_closed
    assert [message["status"] for message in responses if "status" in message] == [499]
    assert not host.gateway_sessions
