import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import asynccontextmanager, closing
from pathlib import Path
from unittest.mock import patch

import anyio

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CORE_DIRECTORY = REPOSITORY_ROOT / "core"
if str(CORE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(CORE_DIRECTORY))

import lite_mcp_runtime as runtime

TOOL_SOURCE = '''from fastmcp.tools import tool

@tool
def echo(value: str) -> str:
    """Return the supplied value."""
    return value
'''


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.tools = self.root / "tools"
        self.tools.mkdir()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def write_manifest(self, source=TOOL_SOURCE):
        tool_path = self.tools / "echo.py"
        tool_path.write_text(source, encoding="utf-8")
        manifest = {
            "version": 1,
            "pythonVersion": "3.14.0",
            "toolsDir": str(self.tools),
            "tools": [
                {
                    "name": "echo",
                    "path": str(tool_path),
                    "sourceFilename": "echo.py",
                    "sourceSha256": hashlib.sha256(source.encode()).hexdigest(),
                    "dependencies": [],
                    "requiresPython": ">=3.14,<3.15",
                }
            ],
        }
        manifest_path = self.root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return manifest_path, tool_path

    def test_loads_exactly_one_decorated_tool(self):
        manifest_path, _ = self.write_manifest()

        tools, details, dependencies = runtime.load_tools(manifest_path)

        self.assertEqual([tool.name for tool in tools], ["echo"])
        self.assertEqual(details[0]["sourceFile"], "echo.py")
        self.assertIn("supplied value", details[0]["description"])
        self.assertEqual(dependencies, [])

    def test_changed_source_aborts_startup(self):
        manifest_path, tool_path = self.write_manifest()
        tool_path.write_text(TOOL_SOURCE + "\n# changed\n", encoding="utf-8")

        with self.assertRaisesRegex(runtime.ToolLoadError, "source changed"):
            runtime.load_tools(manifest_path)

    def test_multiple_tools_abort_startup(self):
        source = (
            TOOL_SOURCE
            + '''
@tool
def second() -> str:
    """Return a second value."""
    return "second"
'''
        )
        manifest_path, _ = self.write_manifest(source)

        with self.assertRaisesRegex(runtime.ToolLoadError, "exactly one"):
            runtime.load_tools(manifest_path)

    def test_database_preview_is_bounded_and_json_safe(self):
        database = self.root / "data.db"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE samples (value BLOB)")
            connection.executemany(
                "INSERT INTO samples(value) VALUES (?)",
                ((bytes([index % 256]),) for index in range(26)),
            )
            connection.commit()

        snapshot = runtime._database_snapshot(database)

        self.assertEqual(snapshot["databasePath"], str(database))
        self.assertEqual(len(snapshot["tables"][0]["rows"]), 25)
        self.assertTrue(snapshot["tables"][0]["truncated"])
        self.assertEqual(snapshot["tables"][0]["rows"][0], ["<blob: 1 bytes>"])

    def test_result_limit_accepts_fastmcp_success_tuple(self):
        from fastmcp.tools import ToolResult

        middleware = runtime.RuntimeGuardMiddleware(60, 60, 1024)
        result = ToolResult(structured_content={"ok": True})

        self.assertIs(middleware._enforce_result_limit(result), result)

    def test_result_limit_returns_structured_nonretryable_error(self):
        from fastmcp.tools import ToolResult

        middleware = runtime.RuntimeGuardMiddleware(60, 60, 8)
        result = middleware._enforce_result_limit(
            ToolResult(structured_content={"value": "too large"})
        )

        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"]["code"], "result_too_large")
        self.assertFalse(result.structured_content["error"]["retryable"])

    def test_timeout_is_structured_and_retryable(self):
        middleware = runtime.RuntimeGuardMiddleware(60, 0.001, 1024)

        async def call_next(_):
            await anyio.sleep(0.1)

        result = anyio.run(middleware.on_call_tool, None, call_next)

        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"]["code"], "tool_timeout")
        self.assertTrue(result.structured_content["error"]["retryable"])

    def test_lite_mcp_error_preserves_retryability(self):
        middleware = runtime.RuntimeGuardMiddleware(0, 60, 1024)

        async def call_next(_):
            raise runtime.LiteMcpError(
                "upstream_busy",
                "Upstream is busy.",
                retryable=True,
                details={"recoveryHint": "Retry shortly."},
            )

        result = anyio.run(middleware.on_call_tool, None, call_next)

        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"]["code"], "upstream_busy")
        self.assertTrue(result.structured_content["error"]["retryable"])

    def test_fastmcp_wrapped_lite_mcp_error_preserves_contract(self):
        from fastmcp import FastMCP
        from fastmcp.tools import tool

        @tool
        def wrapped_failure() -> str:
            """Raise one structured failure through FastMCP."""
            raise runtime.LiteMcpError(
                "upstream_busy",
                "Upstream is busy.",
                retryable=True,
                details={"recoveryHint": "Retry shortly."},
            )

        async def invoke():
            mcp = FastMCP("wrapped-error-test")
            mcp.add_tool(wrapped_failure)
            mcp.add_middleware(runtime.RuntimeGuardMiddleware(0, 60, 4096))
            return await mcp.call_tool("wrapped_failure", {})

        result = anyio.run(invoke)

        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"]["code"], "upstream_busy")
        self.assertTrue(result.structured_content["error"]["retryable"])

    def test_fastmcp_wrapped_value_error_is_internal(self):
        from fastmcp import FastMCP
        from fastmcp.tools import tool

        @tool
        def unexpected_failure() -> str:
            """Raise one unexpected failure through FastMCP."""
            raise ValueError("unexpected")

        async def invoke():
            mcp = FastMCP("wrapped-error-test")
            mcp.add_tool(unexpected_failure)
            mcp.add_middleware(runtime.RuntimeGuardMiddleware(0, 60, 4096))
            return await mcp.call_tool("unexpected_failure", {})

        result = anyio.run(invoke)

        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"]["code"], "internal_error")
        self.assertFalse(result.structured_content["error"]["retryable"])

    def test_system_exit_from_tool_becomes_internal_error(self):
        middleware = runtime.RuntimeGuardMiddleware(0, 60, 1024)

        async def call_next(_):
            raise SystemExit(9)

        result = anyio.run(middleware.on_call_tool, None, call_next)

        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"]["code"], "internal_error")
        self.assertFalse(result.structured_content["error"]["retryable"])

    def test_application_has_startup_only_routes(self):
        manifest_path, _ = self.write_manifest()
        database = self.root / "data.db"
        sqlite3.connect(database).close()
        dashboard = self.root / "dashboard"
        dashboard.mkdir()
        (dashboard / "app.py").write_text("pass\n", encoding="utf-8")
        workspace = self.root / "workspace"
        workspace.mkdir()
        info_page = self.root / "index.html"
        info_page.write_text("<h1>test</h1>", encoding="utf-8")
        runner_path = self.root / "lite_mcp_dashboard_runner.py"
        runner_path.write_text("pass\n", encoding="utf-8")
        environment = {
            "LITE_MCP_MANIFEST": str(manifest_path),
            "LITE_MCP_DB": str(database),
            "LITE_MCP_DASHBOARD_DIR": str(dashboard),
            "LITE_MCP_DASHBOARD_RUNNER": str(runner_path),
            "LITE_MCP_INFO_PAGE": str(info_page),
            "LITE_MCP_WORKSPACE": str(workspace),
            "LITE_MCP_PORT": "8000",
            "LITE_MCP_ALLOWED_HOSTS": '["localhost","127.0.0.1"]',
            "LITE_MCP_ALLOWED_ORIGINS": '["http://localhost:8000"]',
            "LITE_MCP_MAX_CONCURRENCY": "60",
            "LITE_MCP_TOOL_TIMEOUT_SECONDS": "60",
            "LITE_MCP_RESULT_LIMIT_BYTES": "4194304",
            "LITE_MCP_READY_TOKEN": "test-ready-token",
            "LITE_MCP_DEPENDENCIES_JSON": '["fastmcp==4.0.0"]',
        }

        class FakeStreamlitApp:
            def __init__(self, script_path, **kwargs):
                self.script_path = script_path
                self.on_script_error = kwargs["on_script_error"]

            async def __call__(self, scope, receive, send):
                return None

            def lifespan(self):
                @asynccontextmanager
                async def lifespan(_):
                    yield None

                return lifespan

        fake_streamlit = types.SimpleNamespace(App=FakeStreamlitApp)
        fake_dashboard_sdk = types.SimpleNamespace(
            _configure=lambda **_: None,
            preflight_dashboard=lambda _: (),
        )
        previous_reporter = runtime._DASHBOARD_FATAL_REPORTER
        try:
            with (
                patch.dict(os.environ, environment, clear=False),
                patch.dict(sys.modules, {"lite_mcp_dashboard": fake_dashboard_sdk}),
                patch.object(
                    runtime,
                    "_load_and_configure_streamlit",
                    return_value=fake_streamlit,
                ),
            ):
                app = runtime.build_application()
        finally:
            runtime._DASHBOARD_FATAL_REPORTER = previous_reporter

        paths = {route.path for route in app.routes}
        self.assertIn("/info", paths)
        self.assertIn("/healthz", paths)
        self.assertIn("/api/database", paths)
        self.assertIn("/api/runtime", paths)
        self.assertIn("/mcp", paths)
        self.assertIn("/sse", paths)
        self.assertFalse(any("reload" in path for path in paths))
        self.assertEqual(app.routes[-4].path, "/mcp")
        self.assertEqual(app.routes[-3].path, "/mcp/{unsupported_path:path}")
        self.assertEqual(app.routes[-2].path, "/sse")
        self.assertEqual(app.routes[-1].path, "")


if __name__ == "__main__":
    unittest.main()
