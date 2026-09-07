"""The shipped business-plugin asset must work as an actual app on both providers."""

import hashlib
import importlib
import json
import logging
import shutil
import sys
from pathlib import Path

import anyio
import pytest
from fastapi_cordis import PluginUnavailable
from fastmcp import FastMCP
from streamlit.testing.v1 import AppTest
from test_database_providers import provider as provider  # noqa: PLC0414
from toolframe_architecture import validate_source_architecture
from toolframe_artifacts import configure_artifact_store
from toolframe_dashboard import _configure, preflight_app
from toolframe_db import Database
from toolframe_plugins import configure_plugins
from toolframe_runtime import (
    RuntimeGuardMiddleware,
    load_tools,
    validate_domain_llms_source,
)

ASSET = (
    Path(__file__).resolve().parents[2]
    / "skills/toolframe-business-app/assets/business-plugin"
)


def manifest(root, domains):
    tools = []
    for path in sorted(domains.glob("*/tools/*.py")):
        if path.name.startswith("_"):
            continue
        tools.append(
            {
                "name": path.stem,
                "domain": path.parent.parent.name,
                "path": str(path),
                "sourceFilename": path.name,
                "sourceSha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "dependencies": [],
                "requiresPython": ">=3.14,<3.15",
            }
        )
    path = root / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "pythonVersion": "3.14.0",
                "domainsDir": str(domains),
                "tools": tools,
            }
        )
    )
    return path


@pytest.mark.parametrize("domains_name", ["domains", "business"])
def test_stockroom_business_service_mcp_ui_and_recovery(
    provider, tmp_path, monkeypatch, domains_name
):
    app, domains = tmp_path / "app", tmp_path / domains_name
    shutil.copytree(ASSET / "app", app)
    shutil.copytree(ASSET / "domains", domains)
    reporting = domains / "reporting"
    for child in ("model", "tools", "ui", "migrations", "llms"):
        (reporting / child).mkdir(parents=True, exist_ok=True)
    (reporting / "__init__.py").write_text(
        "from .model.summary import total_received\n"
    )
    (reporting / "model/summary.py").write_text(
        "from domains.stockroom import STOCKROOM\n"
        "from toolframe_plugins import use_plugin\n"
        "def total_received():\n"
        "    with use_plugin(STOCKROOM) as stockroom:\n"
        "        return sum(item['quantity'] for item in stockroom.stock_levels())\n"
    )
    with (app / "plugins.py").open("a") as catalog:
        catalog.write("\nDOMAIN_PLUGINS['reporting'] = (PLUGIN.id,)\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in tuple(sys.modules):
        if name == "domains" or name.startswith("domains."):
            sys.modules.pop(name)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifacts = configure_artifact_store(workspace)
    database = Database(provider, ready=False)
    validate_source_architecture(app, domains)
    assert validate_domain_llms_source(app / "llms.txt", domains) == [
        "stockroom/workflow.md"
    ]
    manager = configure_plugins(
        app_dir=app, domains_dir=domains, database=database, artifacts=artifacts
    )
    original_service = importlib.import_module("domains.stockroom.model.service")
    key = importlib.import_module("domains.stockroom").STOCKROOM
    tools, _, _ = load_tools(manifest(tmp_path, domains), manager)
    assert (
        importlib.import_module("domains.stockroom.model.service") is original_service
    )
    _configure(
        database=database,
        app_dir=app,
        domains_dir=domains,
        workspace=workspace,
        logger=logging.getLogger("test.stockroom"),
        runtime={},
        artifacts=artifacts,
        plugins=manager,
    )
    preflight_app(app, domains)
    assert (
        importlib.import_module("domains.stockroom.model.service") is original_service
    )
    ui = AppTest.from_file(app / "dashboard.py", default_timeout=15)
    mcp = FastMCP("stockroom-slice", strict_input_validation=True)
    for tool in tools:
        mcp.add_tool(tool)
    mcp.add_middleware(RuntimeGuardMiddleware(1, 60, 4096))

    def deliver_in_ui():
        ui.run()
        assert not ui.exception
        ui.text_input[0].set_value("UI-1")
        ui.text_input[1].set_value("Notebook")
        ui.number_input[0].set_value(3)
        ui.button[0].click().run()
        assert not ui.exception and not ui.error
        assert ui.success[0].value == "Delivery recorded."
        assert ui.dataframe[0].value.to_dict("records") == [
            {"item": "Notebook", "quantity": 3}
        ]
        ui.button[0].click().run()
        assert ui.success[0].value == "Delivery already recorded."
        ui.number_input[0].set_value(1001)
        ui.button[0].click().run()
        assert not ui.exception
        assert any("stockroom_invalid_delivery" in error.value for error in ui.error)

    async def exercise():
        async with manager.lifespan():
            assert manager.migration_state["appliedNow"] == [
                "stockroom@0000_deliveries"
            ]
            with manager.lease(key) as service:
                assert type(service) is original_service.StockroomService
            await anyio.to_thread.run_sync(deliver_in_ui)
            assert (
                importlib.import_module("domains.stockroom.model.service")
                is original_service
            )
            payload = {"reference": "MCP-1", "item": "Notebook", "quantity": 2}
            first = await mcp.call_tool("stockroom_receive_delivery", payload)
            assert not first.is_error and not first.structured_content["replayed"]
            replay = await mcp.call_tool("stockroom_receive_delivery", payload)
            assert replay.structured_content["replayed"]
            totals = await mcp.call_tool("stockroom_stock_levels", {})
            assert totals.structured_content["items"] == [
                {"item": "Notebook", "quantity": 5}
            ]
            report = importlib.import_module("domains.reporting").total_received
            assert report() == 5
            invalid = await mcp.call_tool(
                "stockroom_receive_delivery", {**payload, "quantity": 0}
            )
            assert (
                invalid.is_error
                and invalid.structured_content["error"]["code"]
                == "stockroom_invalid_delivery"
            )
            mismatch = await mcp.call_tool(
                "stockroom_receive_delivery", {**payload, "quantity": 4}
            )
            assert (
                mismatch.is_error
                and mismatch.structured_content["error"]["code"]
                == "idempotency_key_mismatch"
            )
            assert report() == 5
            await manager.disable("stockroom.local")
            assert manager.snapshot()[-1]["state"] == "blocked"
            assert manager.snapshot()[-1]["enabled"]
            with pytest.raises(PluginUnavailable), manager.lease(key):
                pass
            with pytest.raises(PluginUnavailable):
                report()
            denied = await mcp.call_tool("stockroom_stock_levels", {})
            assert (
                denied.is_error
                and denied.structured_content["error"]["code"] == "plugin_unavailable"
            )
            await anyio.to_thread.run_sync(ui.run)
            assert not ui.exception and not ui.text_input
            assert (
                database.query(
                    "SELECT SUM(quantity) AS total FROM stockroom_deliveries"
                ).iloc[0, 0]
                == 5
            )
            await manager.enable("stockroom.local")
            assert manager.snapshot()[-1]["state"] == "active"
            await anyio.to_thread.run_sync(ui.run)
            assert not ui.exception and ui.dataframe[0].value.iloc[0]["quantity"] == 5
            await manager.disable("stockroom.local", cascade=True)
            await manager.enable("stockroom.local")
            assert manager.snapshot()[-1]["state"] == "disabled"
            await manager.enable("domain.stockroom")
            assert (
                await mcp.call_tool("stockroom_stock_levels", {})
            ).structured_content["items"][0]["quantity"] == 5

    anyio.run(exercise)
