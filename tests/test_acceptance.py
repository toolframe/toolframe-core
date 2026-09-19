"""Failures are exercised across real immutable wheels and worker processes."""

# ruff: noqa: F811 -- shared pytest fixture imported from test_host.
import asyncio
import sqlite3
from pathlib import Path

import httpx
import pytest
import tomlkit
from test_host import fixture_source, host, operate  # noqa: F401
from toolframe_sdk import PluginError

from toolframe_core.api import create_app
from toolframe_core.config import atomic_write


async def until(predicate, timeout=12):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.05)


def store_source(directory, name, *, revision=1, broken=False):
    source = fixture_source(directory, name)
    extra = (
        ', Migration(revision="v2", parent="v1", sql="ALTER TABLE records ADD COLUMN version INTEGER NOT NULL DEFAULT 1;")'
        if revision == 2
        else ""
    )
    (source / "fixture/__init__.py").write_text(f"""import asyncio
from toolframe_sdk import Plugin, PluginDefinition, ServiceSpec, MethodSpec, Requirement, StoreSchema, Migration
plugin=PluginDefinition(id={name!r},factory='fixture:Example',
requires={{'storage':Requirement(service='toolframe.storage.sql')}},
services={{{name + ".service"!r}:ServiceSpec(methods={{'read':MethodSpec(read_only=True),'write':MethodSpec()}})}},
store=StoreSchema(revision='v{revision}',migrations=(Migration(revision='v1',sql='CREATE TABLE records(id INTEGER PRIMARY KEY, label TEXT NOT NULL);'){extra},)))
class Example(Plugin):
    async def activate(self):
        {"raise RuntimeError('candidate failed after migration')" if broken else "pass"}
    async def health(self):
        await self.context.storage().query('SELECT count(*) FROM records')
        return {{'runtime':'ready'}}
    async def invoke(self, service, method, params):
        storage=self.context.storage()
        if method=='read': return await storage.query('SELECT * FROM records ORDER BY id')
        async with storage.transaction(mutation_id=params.get('mutation_id')) as tx:
            await tx.execute('INSERT INTO records(label) VALUES (?)',[params['label']])
            await asyncio.sleep(params.get('delay',0))
        return {{'written':True}}
""")
    return source


async def install_storage(host):
    source = Path(__file__).resolve().parents[2] / "toolframe-storage-sqlite"
    result = await operate(
        host, "add", source=str(source), id="storage-sqlite", enable=True
    )
    assert result["status"] == "succeeded", result


async def test_reviewed_plan_is_immutable_and_rejects_stale_runtime(host, tmp_path):
    source = fixture_source(tmp_path, "alpha")
    assert (await operate(host, "add", source=str(source), enable=True))[
        "status"
    ] == "succeeded"
    old = host.runtimes["alpha"].generation
    fixture_source(tmp_path, "alpha", version="staged")
    updated = await operate(host, "update", id="alpha")
    assert updated["status"] == "succeeded"
    assert host.runtimes["alpha"].generation == old
    planned = await host.wait_operation(
        host.submit("plan", {"action": "reload", "id": "alpha"})["id"]
    )
    assert planned["status"] == "succeeded", planned
    plan = planned["result"]
    assert plan["affected"] == ["alpha"]
    fixture_source(tmp_path, "alpha", version="after-plan")
    result = await operate(host, "reload", id="alpha", plan_id=plan["plan_id"])
    assert result["status"] == "succeeded", result
    assert await host.call_service("alpha", "alpha.service", "echo", {}) == {
        "version": "staged"
    }
    stale = await operate(host, "reload", id="alpha", plan_id=plan["plan_id"])
    assert stale["error"]["code"] == "stale_plan"


async def test_default_validation_checks_resources_and_reports_activation_blockers(
    host, tmp_path
):
    source = fixture_source(tmp_path, "alpha")
    assert (await operate(host, "add", source=str(source), enable=True))[
        "status"
    ] == "succeeded"
    old = host.runtimes["alpha"].generation
    module = source / "fixture/__init__.py"
    module.write_text(
        module.read_text()
        + "\nfrom toolframe_sdk import Resource\nplugin=plugin.model_copy(update={'resources':(Resource(id='bad',kind='mcp.tool',data={'service':'undeclared','method':'read'}),)})\n"
    )
    invalid = await operate(host, "validate", id="alpha")
    assert invalid["error"]["code"] == "resource_target_invalid", invalid
    candidate = host.status()["plugins"][0]["candidate_status"]
    assert candidate["validation"]["preflight"] == "failed"
    assert candidate["validation"]["stages"]["dependency_graph"]["status"] == "failed"
    assert host.runtimes["alpha"].generation == old
    assert await host.call_service("alpha", "alpha.service", "echo", {}) == {
        "version": "old"
    }
    fixture_source(tmp_path, "alpha", dependency="beta")
    blocked = await operate(host, "validate", id="alpha")
    assert blocked["status"] == "succeeded", blocked
    validation = blocked["result"]["validation"]
    assert validation["preflight"] == "passed" and not validation["ready_to_activate"]
    assert validation["blockers"] == {"alpha": ["beta.service"]}
    assert host.runtimes["alpha"].generation == old


async def test_managed_storage_isolation_transaction_drain_scratch_and_retention(
    host, tmp_path
):
    await install_storage(host)
    for name in ("alpha", "beta"):
        source = store_source(tmp_path, name)
        result = await operate(host, "add", source=str(source), enable=True)
        assert result["status"] == "succeeded", result
    old = host.runtimes["alpha"].generation
    call = asyncio.create_task(
        host.call_service(
            "alpha", "alpha.service", "write", {"label": "retained", "delay": 1}
        )
    )
    await until(lambda: bool(host.roots))
    reload = asyncio.create_task(operate(host, "reload", id="alpha"))
    assert await call == {"written": True}
    assert (await reload)["status"] == "succeeded"
    assert host.runtimes["alpha"].generation != old
    assert await host.call_service("beta", "beta.service", "read", {}) == []
    before = {name: runtime.generation for name, runtime in host.runtimes.items()}
    validated = await operate(host, "validate", id="alpha", runtime=True)
    assert validated["status"] == "succeeded", validated
    assert validated["result"]["validation"]["runtime"] == "passed_scratch"
    assert before == {
        name: runtime.generation for name, runtime in host.runtimes.items()
    }
    assert (await operate(host, "remove", id="alpha"))["status"] == "succeeded"
    assert (host.home / "data/plugins/alpha/data.sqlite").is_file()
    assert (await operate(host, "add", source=str(tmp_path / "alpha"), enable=True))[
        "status"
    ] == "succeeded"
    assert await host.call_service("alpha", "alpha.service", "read", {}) == [
        {"id": 1, "label": "retained"}
    ]


async def test_migration_requires_review_and_lost_commit_ack_never_restarts_old_code(
    host, tmp_path, monkeypatch
):
    await install_storage(host)
    source = store_source(tmp_path, "alpha")
    assert (await operate(host, "add", source=str(source), enable=True))[
        "status"
    ] == "succeeded"
    await host.call_service("alpha", "alpha.service", "write", {"label": "survives"})
    original_generation = host.runtimes["alpha"].generation
    store_source(tmp_path, "alpha", revision=2)
    reload = await operate(host, "reload", id="alpha")
    assert reload["error"]["code"] == "migration_required", reload
    assert host.runtimes["alpha"].generation == original_generation
    plan = await operate(host, "migrate-plan", id="alpha")
    assert plan["status"] == "succeeded", plan
    real_admin = host.admin

    async def lost_ack(storage, method, params):
        result = await real_admin(storage, method, params)
        if method == "apply":
            raise PluginError("rpc_timeout", "Commit reply was lost")
        return result

    monkeypatch.setattr(host, "admin", lost_ack)
    applied = await operate(
        host, "migrate-apply", id="alpha", plan_id=plan["result"]["plan_id"]
    )
    assert applied["error"]["code"] == "forward_repair_required", applied
    assert "alpha" not in host.runtimes
    assert host.status()["forward_repair"]
    monkeypatch.setattr(host, "admin", real_admin)
    info = await host.admin(
        host.runtimes["storage-sqlite"], "inspect", {"owner": "alpha"}
    )
    assert info["revision"] == "v2"
    backups = list((host.home / "data/plugins/alpha/backups").glob("*.sqlite"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("SELECT label FROM records").fetchone()[0] == "survives"
    repaired = await operate(host, "reload", id="alpha")
    assert repaired["status"] == "succeeded", repaired
    assert await host.call_service("alpha", "alpha.service", "read", {}) == [
        {"id": 1, "label": "survives", "version": 1}
    ]
    assert host.status()["forward_repair"] is None


async def test_optional_provider_failure_keeps_business_running(host, tmp_path):
    for name in ("alpha", "presentation"):
        source = fixture_source(tmp_path, name, broken=name == "presentation")
        if name == "presentation":
            file = source / "fixture/__init__.py"
            file.write_text(
                file.read_text().replace(
                    "id='presentation',",
                    "id='presentation',kind='provider',provider_kind='llms',",
                )
            )
        assert (await operate(host, "add", source=str(source)))["status"] == "succeeded"
    composition = host.composition.model_copy(deep=True)
    for request in composition.plugins.values():
        request.enabled = True
    atomic_write(host.config.path, tomlkit.dumps(composition.model_dump()))
    result = await operate(host, "apply")
    assert result["error"]["code"] == "optional_provider_failed", result
    assert await host.call_service("alpha", "alpha.service", "echo", {}) == {
        "version": "old"
    }
    assert host.status()["application"] == "degraded"


async def test_business_health_and_expiry_are_separate_from_core_and_optional_readiness(
    host, tmp_path
):
    import time

    source = fixture_source(tmp_path, "alpha")
    module = source / "fixture/__init__.py"
    module.write_text(
        module.read_text().replace(
            "        await asyncio.sleep",
            "        self.unhealthy = params.get('unhealthy', False)\n        await asyncio.sleep",
        )
        + "\n    async def health(self):\n        return {'runtime':'ready', 'business':'failed' if getattr(self,'unhealthy',False) else 'ready'}\n"
    )
    assert (await operate(host, "add", source=str(source), enable=True))[
        "status"
    ] == "succeeded"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(create_app(host, manage_lifespan=False)),
        base_url="http://127.0.0.1:9876",
        headers={"Authorization": f"Bearer {host.token}"},
    ) as client:
        async with host.mutation:
            host.runtimes["alpha"].observed = 0
            stale = host.status()
            assert stale["ready"] and stale["degraded"]
            assert not stale["plugins"][0]["healthy"]
            with pytest.raises(PluginError, match="unavailable"):
                host.begin_root("alpha")
            host.runtimes["alpha"].observed = time.time()
        before = host.runtimes["alpha"].observed
        host.composition.runtime.health_ttl = 0.4
        async with host.mutation:
            await asyncio.sleep(0.8)
            assert host.runtimes["alpha"].observed > before
            assert host.healthy_runtime("alpha")
            assert await host.call_service("alpha", "alpha.service", "echo", {}) == {
                "version": "old"
            }
        await host.call_service("alpha", "alpha.service", "echo", {"unhealthy": True})
        await until(lambda: not host.runtimes["alpha"].admitted)
        report = (await client.get("/_toolframe/v1/status")).json()
        assert report["report_schema"] == 1
        assert report["ready"] and report["degraded"]
        assert report["plugins"][0]["runtime"]["health"]["business"] == "failed"
        assert report["plugins"][0]["runtime"]["health"]["runtime"] == "ready"
        composition = host.composition.model_copy(deep=True)
        composition.required_capabilities = ["service:alpha.service"]
        atomic_write(host.config.path, tomlkit.dumps(composition.model_dump()))
        host.refresh()
        required = await client.get("/_toolframe/v1/readiness")
        assert required.status_code == 503
        assert required.json()["missing_required"] == ["service:alpha.service"]
        assert (await client.get("/healthz")).status_code == 200


async def test_watch_debounces_and_never_enables_disabled_or_manually_stopped_plugin(
    host, tmp_path
):
    source = fixture_source(tmp_path, "alpha")
    assert (await operate(host, "add", source=str(source)))["status"] == "succeeded"
    composition = host.composition.model_copy(deep=True)
    composition.plugins["alpha"].watch = True
    composition.runtime.watch_debounce_ms = 50
    atomic_write(host.config.path, tomlkit.dumps(composition.model_dump()))
    host.refresh()
    await asyncio.sleep(0.2)
    old = host.state.plugin("alpha")["last_candidate"]
    fixture_source(tmp_path, "alpha", version="second")
    await until(lambda: host.state.plugin("alpha")["last_candidate"] != old)
    assert not host.runtimes
    assert (await operate(host, "enable", id="alpha"))["status"] == "succeeded"
    assert (await operate(host, "stop", id="alpha", force=True))[
        "status"
    ] == "succeeded"
    old = host.state.plugin("alpha")["last_candidate"]
    fixture_source(tmp_path, "alpha", version="third")
    await until(lambda: host.state.plugin("alpha")["last_candidate"] != old)
    assert not host.runtimes and host.state.plugin("alpha")["stopped"]


async def test_api_rejects_invalid_requests_and_reports_conflicts(host):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(create_app(host, manage_lifespan=False)),
        base_url="http://127.0.0.1:9876",
        headers={"Authorization": f"Bearer {host.token}"},
    ) as client:
        assert (
            await client.post("/_toolframe/v1/operations", json={})
        ).status_code == 422
        assert (
            await client.post("/_toolframe/v1/operations", json={"action": "add"})
        ).status_code == 422
        assert (
            await client.post(
                "/_toolframe/v1/operations",
                json={"action": "apply", "params": {"expected_revision": "stale"}},
            )
        ).status_code == 409
        exported = await client.get("/_toolframe/v1/composition")
        assert exported.json()["toml"] == host.config.path.read_text()
        request = {
            "action": "apply",
            "params": {"expected_revision": host.config.revision},
        }
        headers = {"Idempotency-Key": "apply-once"}
        first = await client.post(
            "/_toolframe/v1/operations", json=request, headers=headers
        )
        assert first.status_code == 202
        await host.wait_operation(first.json()["id"])
        composition = host.composition.model_copy(deep=True)
        composition.application.name = "Edited after completed operation"
        atomic_write(host.config.path, tomlkit.dumps(composition.model_dump()))
        retried = await client.post(
            "/_toolframe/v1/operations", json=request, headers=headers
        )
        assert retried.status_code == 202
        assert retried.json()["id"] == first.json()["id"]
        request["params"]["watch"] = True
        conflict = await client.post(
            "/_toolframe/v1/operations", json=request, headers=headers
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_conflict"


async def test_descendant_survivor_blocks_replacement_until_explicit_force(
    host, tmp_path
):
    from toolframe_core.process import group_exists

    source = fixture_source(tmp_path, "alpha")
    file = source / "fixture/__init__.py"
    file.write_text(
        file.read_text().replace(
            "        pass",
            "        import subprocess,sys\n        self.child = subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)",
        )
    )
    assert (await operate(host, "add", source=str(source), enable=True))[
        "status"
    ] == "succeeded"
    old = host.runtimes["alpha"]
    result = await operate(host, "reload", id="alpha")
    assert result["error"]["code"] == "cleanup_failed", result
    assert host.runtimes["alpha"] is old and not old.admitted
    assert group_exists(old.worker.pid)
    forced = await operate(host, "stop", id="alpha", force=True)
    assert forced["status"] == "succeeded", forced
    assert not group_exists(old.worker.pid)
    fixture_source(tmp_path, "alpha", version="repaired")
    assert (await operate(host, "enable", id="alpha"))["status"] == "succeeded"


async def test_resume_barrier_never_admits_a_partial_cohort(host):
    from types import SimpleNamespace

    from toolframe_core.host import Runtime

    observations = []

    async def first(method):
        observations.append(
            (host.runtimes["first"].admitted, host.runtimes["second"].admitted)
        )

    async def second(method):
        assert not host.runtimes["first"].admitted
        raise PluginError("resume_failed", "second cannot resume")

    host.runtimes = {
        "first": Runtime("first", "a", {}, worker=SimpleNamespace(request=first)),
        "second": Runtime("second", "b", {}, worker=SimpleNamespace(request=second)),
    }
    host.gated.update(host.runtimes)
    try:
        with pytest.raises(PluginError):
            await host._resume(["first", "second"])
        assert observations == [(False, False)]
        assert all(not r.admitted for r in host.runtimes.values())
        assert host.gated == {"first", "second"}
    finally:
        host.runtimes.clear()
        host.gated.clear()


async def test_cancel_during_drain_retains_generation_and_does_not_cancel_admitted_call(
    host, tmp_path
):
    source = fixture_source(tmp_path, "alpha")
    assert (await operate(host, "add", source=str(source), enable=True))[
        "status"
    ] == "succeeded"
    old = host.runtimes["alpha"].generation
    call = asyncio.create_task(
        host.call_service("alpha", "alpha.service", "echo", {"delay": 2})
    )
    await until(lambda: bool(host.roots))
    operation = host.submit("reload", {"id": "alpha"})
    await until(lambda: host.state.operation(operation["id"])["phase"] == "draining")
    host.cancellations.add(operation["id"])
    host.changed.set()
    result = await host.wait_operation(operation["id"])
    assert result["status"] == "cancelled", result
    assert host.runtimes["alpha"].generation == old and host.runtimes["alpha"].admitted
    assert await call == {"version": "old"}


async def test_manual_toml_edit_during_preparation_preserves_old_generation(
    host, tmp_path, monkeypatch
):
    source = fixture_source(tmp_path, "alpha")
    assert (await operate(host, "add", source=str(source), enable=True))[
        "status"
    ] == "succeeded"
    old = host.runtimes["alpha"].generation
    prepare = host.packages.prepare

    async def edit_while_preparing(*args):
        candidate = await prepare(*args)
        host.config.path.write_text(
            host.config.path.read_text() + "\n# operator edit\n"
        )
        return candidate

    monkeypatch.setattr(host.packages, "prepare", edit_while_preparing)
    result = await operate(host, "reload", id="alpha")
    assert result["error"]["code"] == "stale_plan", result
    assert host.runtimes["alpha"].generation == old
    assert host.config.path.read_text().endswith("# operator edit\n")


async def test_cancelled_transaction_rolls_back_before_root_lease_is_released(
    host, tmp_path
):
    await install_storage(host)
    source = store_source(tmp_path, "alpha")
    assert (await operate(host, "add", source=str(source), enable=True))[
        "status"
    ] == "succeeded"
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            host.call_service(
                "alpha", "alpha.service", "write", {"label": "never", "delay": 5}
            ),
            0.2,
        )
    assert not host.roots
    assert await host.call_service("alpha", "alpha.service", "read", {}) == []


async def test_invalid_composition_keeps_core_management_available(tmp_path):
    from toolframe_core.config import ConfigFile
    from toolframe_core.host import Host

    home = tmp_path / "broken-app"
    ConfigFile(home).initialize(port=12345)
    first = Host(home)
    await first.start()
    await first.close()
    (home / "toolframe.toml").write_text("broken = [")
    second = Host(home)
    try:
        await second.start()
        assert second.composition.application.port == 12345
        assert second.status()["core"] == "ready"
        assert second.status()["application"] == "not_ready"
        assert second.status()["configuration_error"]["code"] == "configuration_invalid"
        assert (home / ".toolframe/endpoint.json").is_file()
    finally:
        await second.close()


async def test_core_crash_reconciles_surviving_worker_before_reactivation(tmp_path):
    import socket
    import sys

    from toolframe_core.config import ConfigFile, PluginRequest
    from toolframe_core.host import Host

    source = fixture_source(tmp_path, "survivor")
    implementation = source / "fixture/__init__.py"
    implementation.write_text(
        implementation.read_text()
        + """\n    async def dispose(self):
        import time
        await asyncio.to_thread(time.sleep, 30)
"""
    )
    home = tmp_path / "crash-app"
    config = ConfigFile(home)
    composition = config.initialize()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        composition.application.port = reservation.getsockname()[1]
    composition.runtime.disposal_timeout = 0.1
    composition.plugins["survivor"] = PluginRequest(source=str(source), enabled=True)
    composition.packages.find_links = [
        str(Path(__file__).resolve().parents[2] / "toolframe-sdk/dist")
    ]
    atomic_write(config.path, tomlkit.dumps(composition.model_dump()))
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "toolframe_core.cli",
        "--home",
        str(home),
        "serve",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    second = None
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{composition.application.port}", trust_env=False
        ) as client:
            async with asyncio.timeout(15):
                while True:
                    try:
                        token = (
                            (home / ".toolframe/management.token").read_text().strip()
                        )
                        status = (
                            await client.get(
                                "/_toolframe/v1/status",
                                headers={"Authorization": "Bearer " + token},
                            )
                        ).json()
                        if status["application"] == "ready":
                            break
                    except httpx.HTTPError, FileNotFoundError:
                        pass
                    await asyncio.sleep(0.05)
        assert status["plugins"][0]["active"]
        worker_pid = status["plugins"][0]["runtime"]["pid"]
        process.kill()
        await process.wait()
        second = Host(home)
        await second.start(apply=False)
        assert second.orphans["survivor"]["pid"] == worker_pid
        result = await operate(second, "enable", id="survivor")
        assert result["error"]["code"] == "cleanup_failed"
        assert not second.runtimes
        result = await operate(second, "stop", id="survivor", force=True)
        assert result["status"] == "succeeded", result
        assert not second.orphans
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        if second:
            if "survivor" in second.orphans:
                await second.stop_orphan("survivor")
            await second.close()
