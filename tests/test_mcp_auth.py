"""Real packaged workers, gateway requests and authenticated FastMCP clients."""

# ruff: noqa: F811 -- shared pytest fixture imported from test_auth.
import asyncio
import json
import time
from shutil import copytree, ignore_patterns

import httpx
import pytest
from fastmcp import Client
from mcp.shared.exceptions import MCPError
from starlette.datastructures import Headers
from test_auth import ROOT, app_host, client, credential, operate  # noqa: F401
from toolframe_sdk import PluginError
from toolframe_sdk.http import HttpServer

from toolframe_core.access import Identity
from toolframe_core.api import create_app


@pytest.fixture
async def mcp_host(app_host, tmp_path):
    # Publish a write tool only in a disposable fixture; the shipped demo stays read-only.
    source = tmp_path / "scoped-mcp-demo"
    copytree(
        ROOT / "toolframe-demo-entry-register",
        source,
        ignore=ignore_patterns(".git", ".venv", "dist", "__pycache__"),
    )
    module = source / "src/toolframe_demo_entry_register/plugin.py"
    module.write_text(
        module.read_text()
        + """
plugin = plugin.model_copy(update={"resources": (*plugin.resources,
    Resource(id="create_entry", kind="mcp.tool", title="Create entry",
        data={"service": SERVICE_NAME, "method": "create_entry",
            "input_schema": plugin.services[SERVICE_NAME].methods["create_entry"].input_schema}),
    Resource(id="admin_list", kind="mcp.tool", title="Restricted list",
        required_scopes=("entry-register.write",),
        data={"service": SERVICE_NAME, "method": "list_entries_tool"}),
)})
"""
    )
    await operate(app_host, "update", id="entry-register", source=str(source))
    await operate(app_host, "reload", id="entry-register")
    for repo in ("toolframe-mcp-auth", "toolframe-fastmcp"):
        await operate(app_host, "add", source=str(ROOT / repo), enable=True)
    yield app_host


async def key(host, **fields):
    return await host.mcp_access.manage(
        Identity(local=True),
        "create",
        {
            "label": "Test client",
            "scopes": ["entry-register.read"],
            **fields,
        },
    )


async def policy(host, mode):
    return await host.mcp_access.manage(
        Identity(local=True),
        "policy",
        {
            "mode": mode,
            "expected_revision": host.config.revision,
        },
    )


def bearer(record):
    return {"Authorization": "Bearer " + record["token"]}


async def rpc(remote, method, params=None, *, headers=None, identifier=1):
    response = await remote.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": identifier,
            "method": method,
            "params": params or {},
        },
        headers={"Accept": "application/json, text/event-stream", **(headers or {})},
    )
    if response.status_code == 200:
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            body = next(
                json.loads(line[6:])
                for line in response.text.splitlines()
                if line.startswith("data: ")
                and json.loads(line[6:]).get("id") == identifier
            )
        else:
            body = response.json()
    else:
        body = None
    return response, body


async def test_management_and_http_authentication_are_separate(mcp_host):
    host = mcp_host
    await policy(host, "protected")
    async with client(host) as remote, client(host, "127.0.0.1") as local:
        for connection in (remote, local):
            missing = await connection.post("/mcp", json={})
            assert missing.status_code == 401
            assert missing.headers["www-authenticate"] == "Bearer"
        issued = await local.post(
            "/_toolframe/v1/mcp-auth/create",
            json={"label": "Laptop", "scopes": ["entry-register.read"]},
        )
        assert (
            issued.status_code == 200 and issued.headers["cache-control"] == "no-store"
        )
        created = issued.json()
        for headers in (
            {"Authorization": "Bearer wrong"},
            {"Authorization": "Basic wrong"},
            {"Authorization": "Bearer " + host.token},
            {"X-Toolframe-Context": "local"},
        ):
            denied = await remote.post("/mcp", json={}, headers=headers)
            assert denied.status_code == 401
        assert (
            await remote.post("/mcp?token=" + created["token"], json={})
        ).status_code == 401
        assert (
            await remote.post(
                "/_toolframe/v1/mcp-auth/list", json={}, headers=bearer(created)
            )
        ).status_code == 401
        assert (
            await remote.get("/_toolframe/v1/status", headers=bearer(created))
        ).status_code == 401
        listing = await local.post("/_toolframe/v1/mcp-auth/list", json={})
        assert created["token"] not in listing.text and "digest" not in listing.text
        # A direct request to the private listener cannot substitute a real external key.
        address = host.runtimes[host.graph.providers["mcp"]].health["http_url"]
        async with httpx.AsyncClient() as direct:
            for headers in ({}, bearer(created), {"Authorization": "Bearer forged"}):
                assert (
                    await direct.post(address + "/mcp", json={}, headers=headers)
                ).status_code == 401
        assert (
            created["token"]
            not in json.dumps(await host.status())
            + json.dumps(await host.state.operations())
            + host.config.path.read_text()
        )
        assert (
            host.redact("accidentally logged " + created["token"])
            == "accidentally logged [REDACTED]"
        )


async def test_real_clients_filter_tools_and_revoke_existing_connections(mcp_host):
    host = mcp_host
    reader = await key(host)
    writer = await key(
        host, label="Writer", scopes=["entry-register.read", "entry-register.write"]
    )
    await policy(host, "protected")
    server = HttpServer(create_app(host, manage_lifespan=False))
    await server.start()
    try:
        async with (
            Client(server.url + "/mcp", auth=reader["token"]) as first,
            Client(server.url + "/mcp", auth=writer["token"]) as second,
        ):
            read_tools, write_tools = await asyncio.gather(
                first.list_tools(), second.list_tools()
            )
            assert {t.name for t in read_tools} < {t.name for t in write_tools}
            read_tool = next(t.name for t in read_tools if "list" in t.name)
            write_tool = next(t.name for t in write_tools if "create" in t.name)
            created = await second.call_tool(
                write_tool, {"label": "Created", "value": 2}
            )
            assert not created.is_error
            assert (await first.call_tool(read_tool, {})).structured_content[
                "count"
            ] == 1
            denied = await first.call_tool(
                write_tool, {"label": "Denied", "value": 3}, raise_on_error=False
            )
            assert denied.is_error
            assert (await second.call_tool(read_tool, {})).structured_content[
                "count"
            ] == 1
            listing = await host.mcp_access.manage(Identity(local=True), "list", {})
            assert all(row["last_used"] is not None for row in listing["keys"])
            await host.mcp_access.manage(
                Identity(local=True), "revoke", {"id": reader["key"]["id"]}
            )
            with pytest.raises(MCPError):
                await first.list_tools()
            assert (await second.call_tool(read_tool, {})).structured_content[
                "count"
            ] == 1
    finally:
        await server.close()
    assert not host.mcp_access.requests


async def test_legacy_sessions_bind_to_key_and_reauthenticate_every_method(mcp_host):
    host = mcp_host
    first, second = await key(host), await key(host)
    await policy(host, "protected")
    async with client(host) as remote:
        response, body = await rpc(
            remote,
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
            headers=bearer(first),
        )
        assert response.status_code == 200, response.text
        assert body["result"]["protocolVersion"] == "2025-11-25"
        session = response.headers["mcp-session-id"]
        headers = {
            **bearer(first),
            "Mcp-Session-Id": session,
            "MCP-Protocol-Version": "2025-11-25",
        }
        initialized = await remote.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream", **headers},
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert initialized.status_code == 202
        listed, body = await rpc(remote, "tools/list", headers=headers)
        assert listed.status_code == 200 and body["result"]["tools"]
        stolen, _ = await rpc(
            remote, "tools/list", headers={**headers, **bearer(second)}
        )
        assert stolen.status_code == 404
        for method in ("GET", "DELETE", "POST"):
            missing = await remote.request(
                method, "/mcp", headers={"Mcp-Session-Id": session}
            )
            assert missing.status_code == 401
        await host.mcp_access.manage(
            Identity(local=True), "revoke", {"id": first["key"]["id"]}
        )
        for method in ("GET", "DELETE", "POST"):
            revoked = await remote.request(method, "/mcp", headers=headers)
            assert revoked.status_code == 401


async def test_request_context_rechecks_permissions_revocation_and_policy(mcp_host):
    host = mcp_host
    issued = await key(host)
    await policy(host, "protected")
    runtime = host.runtimes[host.graph.providers["mcp"]]
    reference = await host.mcp_access.register(runtime, Headers(bearer(issued)))
    resource = next(
        r
        for r in host.graph.resources()
        if r.resource.kind == "mcp.tool" and r.resource.data["method"] == "create_entry"
    )
    params = {
        "key": resource.key,
        "epoch": host.epoch,
        "method": "create_entry",
        "params": {"label": "Forbidden", "value": 1},
        "access_context": reference,
    }
    try:
        # Core refuses even a compromised presentation bypassing discovery filtering.
        with pytest.raises(PluginError) as forbidden:
            await host.worker_call(runtime, "resource.call", params)
        assert forbidden.value.code == "forbidden"
        for forged in ("forged", None, host.access.register(Identity(local=True))):
            with pytest.raises(PluginError):
                await host.mcp_access.resolve(runtime, forged)
        with pytest.raises(PluginError):
            await host.worker_call(
                host.runtimes["entry-register"],
                "mcp.access",
                {"access_context": reference},
            )
        await host.mcp_access.manage(
            Identity(local=True), "revoke", {"id": issued["key"]["id"]}
        )
        with pytest.raises(PluginError) as revoked:
            await host.mcp_access.resolve(runtime, reference)
        assert revoked.value.code == "invalid_credentials"
    finally:
        host.mcp_access.release(reference)
    await policy(host, "public")
    public = await host.mcp_access.register(runtime, Headers())
    assert (await host.mcp_access.resolve(runtime, public)).kind == "public"
    await policy(host, "protected")
    with pytest.raises(PluginError):
        await host.mcp_access.resolve(runtime, public)
    host.mcp_access.release(public)


async def test_provider_failure_disable_reload_and_expiry_never_open_mcp(mcp_host):
    host = mcp_host
    issued = await key(host)
    await policy(host, "protected")
    await operate(host, "reload", id="mcp-auth")
    assert (await host.mcp_access.auth("verify", {"token": issued["token"]}))[
        "principal"
    ]["credential_id"] == issued["key"]["id"]
    async with client(host) as remote:
        await operate(host, "disable", id="mcp-auth")
        for headers in ({}, bearer(issued)):
            assert (
                await remote.post("/mcp", json={}, headers=headers)
            ).status_code == 503
        assert host.config.read()[0].mcp_access.mode == "protected"
        await operate(host, "enable", id="mcp-auth")
        worker = host.runtimes["mcp-auth"].worker
        worker.process.kill()
        await worker.process.wait()
        assert (
            await remote.post("/mcp", json={}, headers=bearer(issued))
        ).status_code == 503
        await operate(host, "reload", id="mcp-auth")
        assert (await host.mcp_access.auth("verify", {"token": issued["token"]}))[
            "principal"
        ]["credential_id"] == issued["key"]["id"]
        short = await key(host, expires_at=time.time() + 0.1)
        await asyncio.sleep(0.15)
        assert (
            await remote.post("/mcp", json={}, headers=bearer(short))
        ).status_code == 401
    # Full host restart retains the credential store and configured protection.
    await host.close()
    from toolframe_core.host import Host

    restarted = await Host.create(host.home)
    await restarted.start()
    try:
        assert restarted.composition.mcp_access.mode == "protected"
        assert (await restarted.mcp_access.auth("verify", {"token": issued["token"]}))[
            "principal"
        ]["credential_id"] == issued["key"]["id"]
    finally:
        await restarted.close()


async def test_remote_admin_cannot_escalate_key_grants(mcp_host):
    host = mcp_host
    expiry = time.time() + 300
    await credential(
        host, scopes=["system.auth.mcp_keys", "entry-register.read"], expires_at=expiry
    )
    identity = Identity(
        session=(await host.access.login("test-password-read", "admin"))["token"]
    )
    catalog = await host.mcp_access.manage(identity, "catalog", {})
    assert list(catalog["scopes"]) == ["entry-register.read"]
    for fields in (
        {"scopes": ["entry-register.write"], "expires_at": expiry - 1},
        {"scopes": ["entry-register.read"]},
    ):
        with pytest.raises(PluginError) as denied:
            await host.mcp_access.manage(
                identity, "create", {"label": "Escalation", **fields}
            )
        assert denied.value.code == "forbidden"
    issued = await host.mcp_access.manage(
        identity,
        "create",
        {
            "label": "Scoped",
            "scopes": ["entry-register.read"],
            "expires_at": expiry - 1,
        },
    )
    assert issued["key"]["created_by"]
    with pytest.raises(PluginError):
        await host.mcp_access.manage(
            identity,
            "policy",
            {"mode": "public", "expected_revision": host.config.revision},
        )
