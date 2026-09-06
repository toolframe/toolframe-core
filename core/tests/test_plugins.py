import hashlib
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

CORE = Path(__file__).resolve().parents[1]
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from fastapi_cordis import DependencyError, PluginError
from fastmcp import FastMCP
from toolframe_dashboard import DashboardContext, DashboardWidget, render_widget
from toolframe_plugins import configure_plugins, use_plugin
from toolframe_runtime import RuntimeGuardMiddleware, load_tools

CATALOG = """from fastapi_cordis import Plugin
from pydantic import BaseModel, ConfigDict

class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prefix: str = "Hello"

async def formatter(ctx):
    return ctx.config.prefix

PLUGINS = [Plugin("formatter", formatter, config_model=Settings)]
DOMAIN_PLUGINS = {"entry_register": ("formatter",)}
"""

TOOL = '''from __future__ import annotations
from fastmcp.tools import tool
from pydantic import BaseModel
from toolframe_plugins import use_plugin

class Payload(BaseModel):
    name: str

@tool
def greet(payload: Payload) -> str:
    """Greet using the shared plugin service."""
    with use_plugin("formatter") as prefix:
        return f"{prefix}, {payload.name}"
'''


class PluginIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.app = self.root / "app"
        self.app.mkdir()
        (self.app / "plugins.py").write_text(CATALOG)
        self.domains = self.root / "domains"
        self.domain = self.domains / "entry_register"
        for child in ("tools", "model", "ui", "migrations", "llms"):
            (self.domain / child).mkdir(parents=True)
        (self.domain / "__init__.py").write_text("")
        tool_path = self.domain / "tools" / "greet.py"
        tool_path.write_text(TOOL)
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "version": 2,
                    "pythonVersion": "3.14.0",
                    "domainsDir": str(self.domains),
                    "tools": [
                        {
                            "name": "greet",
                            "domain": "entry_register",
                            "path": str(tool_path),
                            "sourceFilename": "greet.py",
                            "sourceSha256": hashlib.sha256(TOOL.encode()).hexdigest(),
                            "dependencies": [],
                            "requiresPython": ">=3.14,<3.15",
                        }
                    ],
                }
            )
        )

    def tearDown(self):
        self.temporary.cleanup()

    def manager(self):
        return configure_plugins(
            app_dir=self.app,
            domains_dir=self.domains,
            database=object(),
            artifacts=object(),
        )

    async def test_mcp_and_widget_share_updated_service_and_domain_gate(self):
        manager = self.manager()
        tools, _, _ = load_tools(self.manifest, manager)
        # This also checks preservation of forward annotations in the wrapper.
        mcp = FastMCP("plugin-integration", strict_input_validation=True)
        for tool in tools:
            mcp.add_tool(tool)
        mcp.add_middleware(RuntimeGuardMiddleware(1, 60, 4096))
        rendered = []
        module = ModuleType("widget")

        def render(context):
            self.assertIs(context.plugins, manager)
            with context.plugins.lease("formatter") as value:
                rendered.append(value)

        module.render = render
        widget = DashboardWidget(
            "entry_register.card",
            "Card",
            0,
            self.domain / "ui" / "card.py",
            "widget",
            module,
            "entry_register",
        )
        context = DashboardContext(
            db=SimpleNamespace(audit_sink=None),
            workspace=self.root,
            logger=logging.getLogger("plugin-test"),
            widget_id="",
            runtime={},
            plugins=manager,
        )
        async with manager.lifespan():
            result = await mcp.call_tool("greet", {"payload": {"name": "Ada"}})
            self.assertIn("Hello, Ada", str(result.to_mcp_result()))
            render_widget(widget, context)
            await manager.configure("formatter", {"prefix": "Welcome"})
            result = await mcp.call_tool("greet", {"payload": {"name": "Ada"}})
            self.assertIn("Welcome, Ada", str(result.to_mcp_result()))
            render_widget(widget, context)
            self.assertEqual(rendered, ["Hello", "Welcome"])
            with self.assertRaises(DependencyError):
                await manager.disable("formatter")
            await manager.disable("formatter", cascade=True)
            result = await mcp.call_tool("greet", {"payload": {"name": "Ada"}})
            self.assertTrue(result.is_error)
            self.assertEqual(
                result.structured_content["error"]["code"], "plugin_unavailable"
            )
            self.assertTrue(result.structured_content["error"]["retryable"])
            messages = []
            with patch.dict(
                sys.modules, {"streamlit": SimpleNamespace(info=messages.append)}
            ):
                render_widget(widget, context)
            self.assertEqual(rendered, ["Hello", "Welcome"])
            self.assertIn("domain.entry_register", messages[0])
            await manager.enable("formatter")
            await manager.enable("domain.entry_register")
            with use_plugin("formatter") as value:
                self.assertEqual(value, "Welcome")

    async def test_catalog_validation_and_reserved_names(self):
        for source, expected in [
            ("PLUGINS = [object()]", PluginError),
            (
                "from fastapi_cordis import Plugin\nPLUGINS = [Plugin('domain.fake', lambda _: None)]",
                PluginError,
            ),
            (CATALOG.replace('"entry_register":', '"missing":'), PluginError),
            (CATALOG.replace('("formatter",)', '("missing",)'), DependencyError),
        ]:
            with self.subTest(source=source):
                (self.app / "plugins.py").write_text(source)
                with self.assertRaises(expected):
                    self.manager()

    async def test_existing_apps_without_catalog_receive_domain_plugins(self):
        (self.app / "plugins.py").unlink()
        manager = self.manager()
        async with manager.lifespan():
            self.assertEqual(
                [item["name"] for item in manager.snapshot()],
                [
                    "toolframe.database",
                    "toolframe.artifacts",
                    "domain.entry_register",
                ],
            )
            await manager.disable("domain.entry_register")
            self.assertEqual(manager.snapshot()[-1]["state"], "disabled")
