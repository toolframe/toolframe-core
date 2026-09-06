"""Toolframe host contracts for the embedded reactive lifecycle and management API."""

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi_cordis import DependencyError, Plugin, plugin_router
from test_database_providers import provider as provider  # noqa: PLC0414
from toolframe_db import Database
from toolframe_plugins import configure_plugins, plugin_unavailable_details

CATALOG = """
from fastapi_cordis import Plugin, ServiceKey
from pydantic import BaseModel, ConfigDict, SecretStr

TEXT = ServiceKey("format.text", str)
LABEL = ServiceKey("format.label", str)
class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prefix: str = "Hello"
    fail: bool = False
    secret: SecretStr = "never-expose-cordis-config"

def setup(ctx):
    if ctx.config.fail:
        raise RuntimeError("setup intentionally failed")
    ctx.provide(TEXT, ctx.config.prefix)
    ctx.provide(LABEL, ctx.config.prefix)

PLUGINS = [Plugin("formatter", setup, config_model=Settings, provides=(TEXT, LABEL))]
DOMAIN_PLUGINS = {"alpha": ("format.text",)}
"""


def host(provider, root, *, catalog=CATALOG):
    app = root / "app"
    app.mkdir()
    (app / "plugins.py").write_text(catalog)
    domains = root / "domains"
    (domains / "alpha").mkdir(parents=True)
    manager = configure_plugins(
        app_dir=app,
        domains_dir=domains,
        database=Database(provider, ready=False),
        artifacts=object(),
    )

    @asynccontextmanager
    async def lifespan(_):
        async with manager.lifespan():
            yield

    application = FastAPI(lifespan=lifespan)
    application.include_router(
        plugin_router(manager, prefix="/api/plugins", management=True)
    )
    return manager, TestClient(application)


def states(client):
    response = client.get("/api/plugins")
    assert response.status_code == 200
    assert "never-expose-cordis-config" not in response.text
    return {item["name"]: item for item in response.json()["plugins"]}


@pytest.mark.parametrize("disabled", [False, True])
def test_host_retains_strict_preflight(provider, tmp_path, disabled):
    catalog = (
        CATALOG.replace(
            "provides=(TEXT, LABEL)", "provides=(TEXT, LABEL), enabled=False"
        )
        if disabled
        else CATALOG.replace('("format.text",)', '("missing.service",)')
    )
    with pytest.raises(DependencyError):
        host(provider, tmp_path, catalog=catalog)


def test_api_blocked_recovery_retry_and_noop(provider, tmp_path):
    manager, client = host(provider, tmp_path)
    with client:
        initial = states(client)
        assert initial["formatter"]["provides"] == ["format.text", "format.label"]
        assert "formatter" in initial["domain.alpha"]["requires"]
        assert client.post("/api/plugins/formatter/disable").status_code == 200
        blocked = states(client)["domain.alpha"]
        assert blocked["enabled"] and blocked["state"] == "blocked"
        assert blocked["blockedBy"] == ["formatter"]
        assert client.post("/api/plugins/formatter/enable").status_code == 200
        recovered = states(client)
        assert recovered["domain.alpha"]["state"] == "active"
        plan = client.post(
            "/api/plugins/formatter/config/plan",
            json={"prefix": "Hello", "fail": False},
        ).json()
        assert not plan["changed"] and plan["affected"] == [] and plan["canApply"]
        assert "never-expose" not in str(plan)
        assert (
            client.put(
                "/api/plugins/formatter/config", json={"prefix": "Hello"}
            ).status_code
            == 200
        )
        assert states(client) == recovered
        assert (
            client.post(
                "/api/plugins/formatter/config/plan",
                json={"invalid": "never-expose-cordis-config"},
            ).status_code
            == 422
        )
        changed = client.post(
            "/api/plugins/formatter/config/plan", json={"prefix": "Next"}
        ).json()
        assert changed["changed"] and changed["affected"] == [
            "formatter",
            "domain.alpha",
        ]
        assert (
            client.put("/api/plugins/formatter/config", json={"fail": True}).status_code
            == 500
        )
        failed = states(client)
        assert failed["formatter"]["state"] == "failed"
        assert failed["domain.alpha"]["state"] == "blocked"
        assert (
            "Retry" in plugin_unavailable_details(manager, "formatter")["recoveryHint"]
        )
        assert client.post("/api/plugins/formatter/enable").status_code == 409
        assert (
            client.put(
                "/api/plugins/formatter/config", json={"prefix": "Corrected"}
            ).status_code
            == 200
        )
        assert states(client)["formatter"]["state"] == "failed"
        assert client.post("/api/plugins/formatter/retry").status_code == 200
        retried = states(client)
        assert retried["domain.alpha"]["state"] == "active"
        assert retried["formatter"]["generation"] > recovered["formatter"]["generation"]
        assert retried["formatter"]["configRevision"] == 2
        assert retried["formatter"]["lastSetupMs"] >= 0
        assert retried["formatter"]["lastDrainMs"] >= 0
        assert retried["formatter"]["lastCleanupMs"] >= 0
        assert (
            retried["formatter"]["ownedTasks"]
            == retried["formatter"]["taskFailures"]
            == 0
        )
        assert (
            client.post("/api/plugins/formatter/disable?cascade=true").status_code
            == 200
        )
        assert not states(client)["domain.alpha"]["enabled"]
        assert client.post("/api/plugins/formatter/enable").status_code == 200
        assert states(client)["domain.alpha"]["state"] == "disabled"
        assert client.post("/api/plugins/domain.alpha/enable").status_code == 200
        assert states(client)["domain.alpha"]["state"] == "active"


def test_plan_protects_host_and_apply_revalidates_stale_preview(provider, tmp_path):
    manager, client = host(provider, tmp_path)
    with client:
        name = f"toolframe.database.{provider.dialect}"
        plan = client.post(f"/api/plugins/{name}/config/plan", json={}).json()
        assert plan["protected"] == [name] and not plan["canApply"]
        for action in ("disable", "enable", "retry"):
            assert client.post(f"/api/plugins/{name}/{action}").status_code == 409
        preview = client.post(
            "/api/plugins/formatter/config/plan", json={"prefix": "Next"}
        ).json()
        assert preview["canApply"]
        client.portal.call(
            manager.register,
            Plugin(
                "protected.consumer",
                lambda ctx: ctx.require("format.text"),
                requires=("format.text",),
                managed=False,
            ),
        )
        before = states(client)
        assert (
            client.put(
                "/api/plugins/formatter/config", json={"prefix": "Next"}
            ).status_code
            == 409
        )
        assert states(client) == before
        updated = client.post(
            "/api/plugins/formatter/config/plan", json={"prefix": "Next"}
        ).json()
        assert (
            updated["protected"] == ["protected.consumer"] and not updated["canApply"]
        )


def test_scoped_catalog_ids_have_management_routes_and_schema_mapping(
    provider, tmp_path
):
    catalog = """
from fastapi_cordis import Plugin
from toolframe_migrations import Schema, Revision
from pathlib import Path
path = Path(__file__).parent.parent / "initial.sql"
PLUGINS = [Plugin("service", lambda ctx: "ready", scope="example")]
SCHEMAS = {"example/service": Schema("scoped", (Revision("1", {"sqlite":path, "postgresql":path}, ("scoped_entries",)),), "1")}
"""
    (tmp_path / "initial.sql").write_text(
        "CREATE TABLE scoped_entries (id INTEGER PRIMARY KEY);"
    )
    _, client = host(provider, tmp_path, catalog=catalog)
    with client:
        assert states(client)["example/service"]["requires"] == [
            f"toolframe.database.{provider.dialect}"
        ]
        assert client.post("/api/plugins/example%2Fservice/disable").status_code == 200
        assert client.post("/api/plugins/example%2Fservice/enable").status_code == 200
