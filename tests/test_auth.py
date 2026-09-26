"""Authorization across real packaged plugin workers and HTTP admission."""

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import tomlkit
from toolframe_sdk import PluginError, Principal

from toolframe_core.access import Identity
from toolframe_core.api import create_app
from toolframe_core.config import ConfigFile, atomic_write
from toolframe_core.host import Host
from toolframe_core.scaffold import new_plugin

ROOT = Path(__file__).resolve().parents[2]


async def operate(host, action, **params):
    result = await host.wait_operation((await host.submit(action, params))["id"])
    assert result["status"] == "succeeded", result.get("error")
    return result


@pytest.fixture
async def app_host(tmp_path):
    config = ConfigFile(tmp_path)
    composition = config.initialize(host="0.0.0.0")
    composition.packages.find_links = [str(ROOT / "toolframe-sdk/dist")]
    composition.runtime.health_interval = 0.2
    atomic_write(config.path, tomlkit.dumps(composition.model_dump()))
    host = await Host.create(tmp_path)
    await host.start()
    try:
        for repo in (
            "toolframe-storage-sqlite",
            "toolframe-auth",
            "toolframe-dashboard-auth",
            "toolframe-demo-entry-register",
        ):
            await operate(host, "add", source=str(ROOT / repo), enable=True)
        yield host
    finally:
        await host.close()


def client(host, peer="192.168.1.20"):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(
            create_app(host, manage_lifespan=False), client=(peer, 54321)
        ),
        base_url="http://192.168.1.10:9876",
    )


async def credential(host, **fields):
    return await host.access.manage(
        Identity(local=True),
        "create",
        {
            "label": "Read-only",
            "password": "test-password-read",
            "scopes": ["entry-register.read"],
            "expected_revision": host.config.revision,
            **fields,
        },
    )


async def test_public_default_protected_scopes_public_api_and_recovery(app_host):
    host = app_host
    async with client(host) as remote, client(host, "127.0.0.1") as local:
        context = (await remote.get("/_toolframe/auth/context")).json()
        assert context["mode"] == "public" and context["actor"] == "anonymous"
        assert "entry-register.write" in context["allowed_scopes"]
        assert not any(s.startswith("system.") for s in context["allowed_scopes"])
        assert (await remote.get("/_toolframe/v1/status")).status_code == 401
        assert (await local.get("/_toolframe/v1/status")).status_code == 200
        assert (
            await local.get(
                "/_toolframe/auth/context",
                headers={"Authorization": "Bearer " + host.token},
            )
        ).json()["local_control"]
        async with client(host, "::1") as ipv6:
            assert (await ipv6.get("/_toolframe/auth/context")).json()["local_control"]
        host.composition.access.local_control = False
        assert (await local.get("/_toolframe/v1/status")).status_code == 401
        host.composition.access.local_control = True
        for spoof in (
            {"X-Forwarded-For": "127.0.0.1"},
            {"Host": "127.0.0.1:9876", "X-Toolframe-Context": "local"},
        ):
            assert (
                await remote.get("/_toolframe/v1/status", headers=spoof)
            ).status_code == 401
        record = await credential(host)
        assert record["access"]["mode"] == "protected"
        assert (await remote.get("/_toolframe/auth/login")).status_code == 200
        invalid = await remote.post(
            "/_toolframe/auth/login",
            json={"password": "wrong"},
            headers={"Accept": "application/json"},
        )
        assert (
            invalid.status_code == 401
            and (await host.access.mode())["mode"] == "protected"
        )
        login = await remote.post(
            "/_toolframe/auth/login",
            json={"password": "test-password-read"},
            headers={"Accept": "application/json"},
        )
        assert (
            login.status_code == 200
            and "httponly" in login.headers["set-cookie"].lower()
        )
        base = "/_toolframe/v1/services/entry-register/entry-register.entries/"
        assert (await remote.post(base + "list_entries", json={})).status_code == 200
        assert (
            await remote.post(
                base + "create_entry", json={"label": "Denied", "value": 1}
            )
        ).status_code == 403
        assert (
            await local.post(
                base + "create_entry", json={"label": "Allowed", "value": 2}
            )
        ).status_code == 200
        public = "/api/entry-register/entry-register.entries/"
        assert (await remote.post(public + "list_entries", json={})).json()[
            "count"
        ] == 1
        assert (
            await remote.post(
                public + "create_entry", json={"label": "Denied", "value": 1}
            )
        ).status_code == 403
        assert (
            await remote.post(
                "/_toolframe/v1/services/auth/toolframe.auth/list", json={}
            )
        ).status_code == 403
        assert (
            await local.post(
                "/_toolframe/v1/services/storage-sqlite/toolframe.storage.sql/query",
                json={"sql": "SELECT 1"},
            )
        ).status_code == 403
        await host.access.manage(
            Identity(local=True), "revoke", {"id": record["credential"]["id"]}
        )
        assert (await remote.post(base + "list_entries", json={})).status_code == 401
        assert (await host.access.mode())["mode"] == "protected"
        assert (await remote.post(public + "list_entries", json={})).status_code == 200

        before = host.config.read()[0].access.mode
        await operate(host, "disable", id="auth")
        report = (await remote.get("/_toolframe/auth/context")).json()
        assert report["mode"] == "public" and report["reason"] == "auth_unavailable"
        assert (
            await remote.post(
                base + "create_entry", json={"label": "Public", "value": 3}
            )
        ).status_code == 200
        assert (await remote.get("/_toolframe/v1/status")).status_code == 401
        assert host.config.read()[0].access.mode == before == "protected"
        await operate(host, "enable", id="auth")
        assert (await host.access.mode())["mode"] == "protected"
        assert (await remote.post(base + "list_entries", json={})).status_code == 401


async def test_browser_context_isolation_and_admitted_call_survives_disconnect(
    app_host, monkeypatch
):
    host = app_host
    record = await credential(host)
    login = await host.access.login("test-password-read", "peer")
    reference = host.access.register(Identity(session=login["token"]))
    local_reference = host.access.register(Identity(local=True))
    dashboard = SimpleNamespace(
        name="test-dashboard",
        candidate={"definition": {"provider_kind": "dashboard"}},
        epoch=host.epoch,
    )
    host.runtimes[dashboard.name] = dashboard
    resource = next(
        item
        for item in host.graph.resources()
        if item.owner == "entry-register" and item.resource.kind == "dashboard.page"
    )
    params = {
        "key": resource.key,
        "epoch": host.epoch,
        "method": "entry-register.entries@1/list_entries",
        "access_context": reference,
        "params": {},
    }
    started, finish = asyncio.Event(), asyncio.Event()
    completed = asyncio.get_running_loop().create_future()
    original = host.invoke

    async def delayed(root, target, service, method, payload, **kwargs):
        if target == "entry-register" and method == "list_entries":
            started.set()
            await finish.wait()
            # A nested write is still outside this root's read-only grant.
            with pytest.raises(PluginError, match="caller does not have"):
                await original(
                    root, target, service, "create_entry", {"label": "No", "value": 1}
                )
            result = await original(root, target, service, method, payload, **kwargs)
            completed.set_result(result)
            return result
        return await original(root, target, service, method, payload, **kwargs)

    try:
        for context in (reference, "forged"):
            with pytest.raises(PluginError):
                await host.worker_call(
                    dashboard,
                    "management.call",
                    {
                        "action": "status",
                        "access_context": context,
                    },
                )
        # The same provider keeps local and remote contexts separate.
        assert host.access.context(local_reference).local
        assert not host.access.context(reference).local
        monkeypatch.setattr(host, "invoke", delayed)
        task = asyncio.create_task(host.worker_call(dashboard, "resource.call", params))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await host.access.manage(
            Identity(local=True), "revoke", {"id": record["credential"]["id"]}
        )
        assert any(root.owner == "entry-register" for root in host.roots.values())
        with pytest.raises(PluginError):
            await host.worker_call(dashboard, "resource.call", params)
        finish.set()
        assert (await asyncio.wait_for(completed, 5))["count"] == 0
        await host._drain({"entry-register"}, 5)
    finally:
        finish.set()
        host.runtimes.pop(dashboard.name)
        host.access.contexts.pop(reference)
        host.access.contexts.pop(local_reference)


async def test_reload_sessions_secrets_and_normal_transition_do_not_open(app_host):
    host = app_host
    await credential(host)
    login = await host.access.login("test-password-read", "test-peer")
    identity = Identity(session=login["token"])
    key = host.home / ".toolframe/secrets/auth/password-lookup.key"
    before = key.read_bytes()
    assert key.stat().st_mode & 0o077 == 0
    observed = []
    running = True

    async def observe():
        while running:
            observed.append((await host.access.mode())["mode"])
            await asyncio.sleep(0.01)

    task = asyncio.create_task(observe())
    try:
        await operate(host, "reload", id="auth")
    finally:
        running = False
        await task
    assert "public" not in observed and "waiting" in observed
    assert key.read_bytes() == before
    assert (
        await host.access.admit(identity, ["entry-register.read"])
    ).kind == "password"
    assert "test-password-read" not in json.dumps(
        await host.status()
    ) + host.config.path.read_text() + json.dumps(await host.state.operations())
    assert login["token"] not in json.dumps(await host.status()) + json.dumps(
        await host.state.operations()
    )
    # A crashed verifier selects fallback, then reload proves process-group
    # cleanup and restores protection without changing business workers/data.
    auth_worker = host.runtimes["auth"].worker
    business_pid = host.runtimes["entry-register"].worker.pid
    auth_worker.process.kill()
    await auth_worker.process.wait()
    assert (await host.access.mode())["reason"] == "auth_unavailable"
    await operate(host, "reload", id="auth")
    assert (await host.access.mode())["mode"] == "protected"
    assert host.runtimes["entry-register"].worker.pid == business_pid
    assert key.read_bytes() == before
    assert (
        await host.access.admit(identity, ["entry-register.read"])
    ).kind == "password"
    # A missing retained key is an explicit activation failure; it is never silently replaced.
    key.unlink()
    result = await host.wait_operation(
        (await host.submit("reload", {"id": "auth"}))["id"]
    )
    assert result["status"] != "succeeded"
    assert not key.exists()
    assert (await host.access.mode())["reason"] == "auth_unavailable"


async def test_queued_management_rechecks_revocation_and_delegation_uses_own_grant(
    app_host,
):
    host = app_host
    record = await credential(
        host,
        scopes=[
            "system.plugins.manage",
            "system.auth.passwords",
            "entry-register.read",
        ],
        expires_at=time.time() + 300,
    )
    login = await host.access.login("test-password-read", "test-peer")
    identity = Identity(session=login["token"])
    # Public mode must not enlarge a credential administrator's delegation authority.
    await host.access.manage(
        Identity(local=True),
        "policy",
        {"mode": "public", "expected_revision": host.config.revision},
    )
    with pytest.raises(PluginError, match="beyond your own grant"):
        await host.access.manage(
            identity,
            "create",
            {
                "label": "Escalate",
                "scopes": ["entry-register.write"],
                "expires_at": time.time() + 100,
                "expected_revision": host.config.revision,
            },
        )
    async with host.mutation:
        operation = await host.submit(
            "disable", {"id": "entry-register"}, identity=identity
        )
        await host.access.auth(
            "revoke",
            {"id": record["credential"]["id"]},
            principal=Principal(kind="local"),
        )
    result = await host.wait_operation(operation["id"])
    assert result["status"] == "failed"
    assert host.healthy_runtime("entry-register")


async def test_verifier_fault_falls_back_but_expected_denials_do_not(
    app_host, monkeypatch
):
    host = app_host
    await credential(host)
    for password in (None, [], "wrong", "\ud800" * 12):
        with pytest.raises(PluginError):
            await host.access.login(password, "peer")
        assert (await host.access.mode())["mode"] == "protected"
    host.access.login_attempts["limited-peer"] = (time.monotonic(), 10)
    with pytest.raises(PluginError) as limited:
        await host.access.login("test-password-read", "limited-peer")
    assert limited.value.code == "rate_limited"
    assert (await host.access.mode())["mode"] == "protected"
    original = host.call_service

    async def broken(owner, service, method, params, **kwargs):
        if service == "toolframe.auth":
            return {"principal": {"kind": "operator"}}
        return await original(owner, service, method, params, **kwargs)

    monkeypatch.setattr(host, "call_service", broken)
    with pytest.raises(PluginError) as fault:
        await host.access.login("test-password-read", "peer")
    assert fault.value.code == "auth_unavailable"
    assert (await host.access.mode())["reason"] == "auth_unavailable"
    assert not host.healthy_runtime("auth")
    assert (await host.access.presentation(Identity()))["mode"] == "public"
    with pytest.raises(PluginError):
        await host.access.admit(Identity(), ["system.plugins.manage"])


async def test_first_password_policy_write_failure_returns_recoverable_secret(
    app_host, monkeypatch
):
    host = app_host

    def changed(*args):
        raise PluginError("stale_revision", "Composition was changed externally")

    monkeypatch.setattr(host.config, "edit", changed)
    result = await credential(host, password=None)
    assert result["credential"]["id"] and len(result["password"]) >= 32
    assert result["policy_error"] and result["access"]["mode"] == "public"
    assert host.composition.access.mode == "public"
    assert result["password"] not in json.dumps(await host.state.operations())
    assert result["password"] not in host.config.path.read_text()
    login = await host.access.login(result["password"], "recovery-peer")
    assert login["principal"]["credential_id"] == result["credential"]["id"]


async def test_drain_retries_cancelled_probe_cleanup(app_host, monkeypatch):
    host = app_host
    root = host.begin_root("entry-register")
    entered = asyncio.Event()
    original = host.admin

    async def interrupted(runtime, method, params):
        if method == "cleanup_root" and params["root_id"] == root.id:
            entered.set()
            await asyncio.Future()
        return await original(runtime, method, params)

    monkeypatch.setattr(host, "admin", interrupted)
    cleanup = asyncio.create_task(host.end_root(root))
    await asyncio.wait_for(entered.wait(), 5)
    cleanup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cleanup
    assert root.id in host.failed_roots and root.id in host.roots
    monkeypatch.setattr(host, "admin", original)
    await host._drain({"entry-register"}, 2)
    assert root.id not in host.failed_roots and root.id not in host.roots


async def test_dynamic_grant_includes_newly_installed_business_scopes(app_host):
    host = app_host
    await credential(host)
    explicit = Identity(
        session=(await host.access.login("test-password-read", "reader"))["token"]
    )
    await credential(
        host, password="dynamic-business-secret", scopes=[], all_business=True
    )
    dynamic = Identity(
        session=(await host.access.login("dynamic-business-secret", "dynamic"))["token"]
    )
    created = new_plugin(host.home, "future-notes")
    await operate(host, "add", source=created["path"], enable=True)
    actor = await host.access.admit(dynamic, ["future-notes.write"])
    row = await host.call_service(
        "future-notes",
        "future-notes.notes",
        "capture",
        {"body": "New business grant"},
        principal=actor,
    )
    assert row["body"] == "New business grant"
    with pytest.raises(PluginError) as missing:
        await host.access.admit(explicit, ["future-notes.write"])
    assert missing.value.code == "forbidden"
    with pytest.raises(PluginError):
        await host.access.admit(dynamic, ["system.plugins.manage"])
