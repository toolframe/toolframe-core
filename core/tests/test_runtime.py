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
from fastapi.testclient import TestClient

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CORE_DIRECTORY = REPOSITORY_ROOT / "core"
if str(CORE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(CORE_DIRECTORY))

import lite_mcp_runtime as runtime
from lite_mcp_idempotency import IdempotencyError

TOOL_SOURCE = '''from fastmcp.tools import tool

@tool
def echo(value: str) -> str:
    """Return the supplied value."""
    return value
'''

DATABASE_CONTEXT_TOOL_SOURCE = '''from fastmcp.tools import tool
from lite_mcp_db import get_database

DATABASE = get_database()

@tool
def echo(value: str) -> str:
    """Return the supplied value after startup database configuration."""
    assert DATABASE.path.name == "data.db"
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

    def write_llms_source(self, *, index=None, documents=None):
        llms_dir = self.root / "llms"
        llms_dir.mkdir(exist_ok=True)
        documents = documents or {"overview": "# Overview\n\nDetails.\n"}
        for name, contents in documents.items():
            (llms_dir / f"{name}.md").write_text(contents, encoding="utf-8")
        index_path = self.root / "llms.txt"
        index_path.write_text(
            index
            or "# lite-mcp\n> LAN MCP server context.\n\n## Documents\n\n- [Overview](/llms/overview.md)\n",
            encoding="utf-8",
        )
        return index_path, llms_dir

    def test_validate_llms_source_returns_safe_reachable_document_allowlist(self):
        index_path, llms_dir = self.write_llms_source(
            index=(
                "# lite-mcp\n"
                "> LAN MCP server context.\n"
                "\n"
                "## Documents\n"
                "\n"
                "- [Overview](/llms/overview.md)\n"
                "- [Workflow](/llms/workflow_notes.md)\n"
            ),
            documents={
                "overview": "# Overview\n\nDetails.\n",
                "workflow_notes": "# Workflow\n\nDetails.\n",
            },
        )

        self.assertEqual(
            runtime.validate_llms_source(index_path, llms_dir),
            ["overview.md", "workflow_notes.md"],
        )

    def test_validate_llms_source_accepts_index_inside_details_directory(self):
        index_path, llms_dir = self.write_llms_source()
        nested_index_path = llms_dir / "llms.txt"
        index_path.replace(nested_index_path)

        self.assertEqual(
            runtime.validate_llms_source(nested_index_path, llms_dir),
            ["overview.md"],
        )

    def test_validate_llms_source_accepts_v2_bom_and_blank_line_before_summary(self):
        index_path, llms_dir = self.write_llms_source(
            index=(
                "\ufeff# lite-mcp\n"
                "\n"
                "> LAN MCP server context.\n"
                "\n"
                "## Documents\n"
                "\n"
                "- [Overview](/llms/overview.md)\n"
            )
        )

        self.assertEqual(runtime.validate_llms_source(index_path, llms_dir), ["overview.md"])

    def test_validate_llms_source_rejects_unlinked_or_unsafe_documents(self):
        index_path, llms_dir = self.write_llms_source(
            documents={
                "overview": "# Overview\n\nDetails.\n",
                "orphan": "# Orphan\n\nNot linked.\n",
            }
        )

        with self.assertRaisesRegex(ValueError, "orphan.md.*not reachable"):
            runtime.validate_llms_source(index_path, llms_dir)

        unsafe = llms_dir / "Unsafe.md"
        unsafe.write_text("# Unsafe\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unsafe.md.*safe"):
            runtime.validate_llms_source(index_path, llms_dir)

    def test_validate_llms_source_rejects_symlink_utf8_and_oversize_sources(self):
        index_path, llms_dir = self.write_llms_source()
        target = self.root / "outside.md"
        target.write_text("# Outside\n", encoding="utf-8")
        (llms_dir / "overview.md").unlink()
        (llms_dir / "overview.md").symlink_to(target)
        with self.assertRaisesRegex(ValueError, "overview.md.*symlink"):
            runtime.validate_llms_source(index_path, llms_dir)

        (llms_dir / "overview.md").unlink()
        (llms_dir / "overview.md").write_bytes(b"# Overview\n\xff")
        with self.assertRaisesRegex(ValueError, "overview.md.*UTF-8"):
            runtime.validate_llms_source(index_path, llms_dir)

        (llms_dir / "overview.md").write_bytes(b"x" * (256 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, "overview.md.*256 KiB"):
            runtime.validate_llms_source(index_path, llms_dir)

    def test_loads_exactly_one_decorated_tool(self):
        manifest_path, _ = self.write_manifest()

        tools, details, dependencies = runtime.load_tools(manifest_path)

        self.assertEqual([tool.name for tool in tools], ["echo"])
        self.assertEqual(details[0]["sourceFile"], "echo.py")
        self.assertIn("supplied value", details[0]["description"])
        self.assertEqual(dependencies, [])

    def test_tools_can_import_configured_artifact_store_at_startup(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        runtime.configure_artifact_store(workspace)
        source = '''from fastmcp.tools import tool
from lite_mcp_artifacts import get_artifact_store

STORE = get_artifact_store()

@tool
def echo(value: str) -> str:
    """Return the supplied value."""
    assert STORE.root.name == "artifacts"
    return value
'''
        manifest_path, _ = self.write_manifest(source)

        tools, _, _ = runtime.load_tools(manifest_path)

        self.assertEqual([tool.name for tool in tools], ["echo"])

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

    def test_idempotency_error_preserves_structured_contract(self):
        middleware = runtime.RuntimeGuardMiddleware(0, 60, 4096)
        error = IdempotencyError(
            "idempotency_key_mismatch",
            "The key was reused with different input.",
            recovery_hint="Choose a new key.",
        )

        async def call_next(_):
            raise error

        result = anyio.run(middleware.on_call_tool, None, call_next)

        self.assertTrue(result.is_error)
        self.assertEqual(
            result.structured_content["error"]["code"],
            "idempotency_key_mismatch",
        )
        self.assertFalse(result.structured_content["error"]["retryable"])
        self.assertEqual(result.structured_content["error"]["traceId"], error.trace_id)

    def test_database_error_preserves_structured_contract(self):
        middleware = runtime.RuntimeGuardMiddleware(0, 60, 4096)
        error = runtime.database_sdk.DatabaseError(
            "database_busy",
            "The database is busy.",
            retryable=True,
            recovery_hint="Wait briefly, then retry once.",
        )

        async def call_next(_):
            raise error

        result = anyio.run(middleware.on_call_tool, None, call_next)

        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"]["code"], "database_busy")
        self.assertTrue(result.structured_content["error"]["retryable"])
        self.assertEqual(
            result.structured_content["error"]["details"]["recoveryHint"],
            "Wait briefly, then retry once.",
        )
        self.assertEqual(result.structured_content["error"]["traceId"], error.trace_id)

    def test_artifact_error_preserves_structured_contract(self):
        from lite_mcp_artifacts import ArtifactExistsError

        middleware = runtime.RuntimeGuardMiddleware(0, 60, 1024)

        async def call_next(_):
            raise ArtifactExistsError(
                "An artifact already exists.", details={"path": "report.csv"}
            )

        result = anyio.run(middleware.on_call_tool, None, call_next)

        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"]["code"], "artifact_exists")
        self.assertFalse(result.structured_content["error"]["retryable"])
        self.assertEqual(
            result.structured_content["error"]["details"]["path"], "report.csv"
        )

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
        manifest_path, _ = self.write_manifest(DATABASE_CONTEXT_TOOL_SOURCE)
        llms_file, llms_dir = self.write_llms_source()
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
            "LITE_MCP_LLMS_FILE": str(llms_file),
            "LITE_MCP_LLMS_DIR": str(llms_dir),
            "LITE_MCP_PORT": "8000",
            "LITE_MCP_ALLOWED_HOSTS": '["localhost","127.0.0.1"]',
            "LITE_MCP_ALLOWED_ORIGINS": '["http://localhost:8000"]',
            "LITE_MCP_MAX_CONCURRENCY": "60",
            "LITE_MCP_TOOL_TIMEOUT_SECONDS": "60",
            "LITE_MCP_RESULT_LIMIT_BYTES": "4194304",
            "LITE_MCP_READY_TOKEN": "test-ready-token",
            "LITE_MCP_DEPENDENCIES_JSON": '["fastmcp==4.0.0"]',
            "LITE_MCP_LOGS_DIR": str(self.root / "logs"),
            "LITE_MCP_LOG_RETENTION_DAYS": "14",
            "LITE_MCP_LOG_MAX_TOTAL_MIB": "256",
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
        self.assertIn("/llms.txt", paths)
        self.assertIn("/llms.txt/{unsupported_path:path}", paths)
        self.assertIn("/llms/{document_name}.md", paths)
        self.assertIn("/llms", paths)
        self.assertIn("/llms/{unsupported_path:path}", paths)
        self.assertIn("/mcp", paths)
        self.assertIn("/sse", paths)
        self.assertFalse(any("reload" in path for path in paths))
        self.assertEqual(app.routes[-4].path, "/mcp")
        self.assertEqual(app.routes[-3].path, "/mcp/{unsupported_path:path}")
        self.assertEqual(app.routes[-2].path, "/sse")
        self.assertEqual(app.routes[-1].path, "")

        client = TestClient(app, base_url="http://localhost")
        root_response = client.get("/llms.txt")
        self.assertEqual(root_response.status_code, 200)
        self.assertEqual(root_response.content, llms_file.read_bytes())
        self.assertEqual(root_response.headers["content-type"], "text/markdown; charset=utf-8")
        self.assertEqual(root_response.headers["cache-control"], "no-store")
        self.assertEqual(root_response.headers["x-content-type-options"], "nosniff")

        llms_file.write_text(
            "# lite-mcp changed\n"
            "> Updated LAN MCP server context.\n"
            "\n"
            "## Documents\n"
            "\n"
            "- [Overview](/llms/overview.md)\n",
            encoding="utf-8",
        )
        self.assertEqual(client.get("/llms.txt").content, llms_file.read_bytes())

        detail_response = client.get("/llms/overview.md")
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(detail_response.headers["link"], '</llms.txt>; rel="describedby"')
        self.assertEqual(detail_response.content, b"# Overview\n\nDetails.\n")

        (llms_dir / "overview.md").write_bytes(b"# Overview\n\nChanged.\n")
        self.assertEqual(client.get("/llms/overview.md").content, b"# Overview\n\nChanged.\n")
        head_response = client.head("/llms.txt")
        self.assertEqual(head_response.status_code, 200)
        self.assertEqual(head_response.content, b"")
        self.assertEqual(head_response.headers["content-type"], "text/markdown; charset=utf-8")
        self.assertEqual(client.get("/llms/unknown.md").status_code, 404)
        self.assertEqual(client.get("/llms/%2E%2E/outside.md").status_code, 404)
        self.assertEqual(client.get("/llms").status_code, 404)
        method_rejected = client.post("/llms.txt")
        self.assertEqual(method_rejected.status_code, 405)
        self.assertEqual(method_rejected.headers["allow"], "GET, HEAD")
        self.assertEqual(client.put("/llms.txt").status_code, 405)
        self.assertEqual(client.get("/llms.txt/").status_code, 404)
        self.assertEqual(client.get("/llms.txt/extra").status_code, 404)
        self.assertEqual(client.request("PROPFIND", "/llms.txt").status_code, 405)
        self.assertEqual(
            client.request("PROPFIND", "/llms.txt/extra").status_code, 404
        )
        self.assertEqual(client.request("FOO", "/llms/overview.md").status_code, 404)

        (llms_dir / "overview.md").write_bytes(b"# Overview\n\xff")
        invalid = client.get("/llms/overview.md")
        self.assertEqual(invalid.status_code, 503)
        self.assertNotIn("Overview", invalid.text)

        (llms_dir / "overview.md").write_bytes(b"# Overview\n\nChanged.\n")
        (llms_dir / "overview.md").unlink()
        unavailable = client.get("/llms/overview.md")
        self.assertEqual(unavailable.status_code, 503)
        self.assertNotIn("Overview", unavailable.text)

        # This test does not enter the ASGI lifespan, so release the process lock.
        for middleware in app.user_middleware:
            if middleware.cls is runtime.McpTransportMiddleware:
                middleware.kwargs["audit_sink"].close()
                break


if __name__ == "__main__":
    unittest.main()
