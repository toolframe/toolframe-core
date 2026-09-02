import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CORE_DIRECTORY = REPOSITORY_ROOT / "core"
if str(CORE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(CORE_DIRECTORY))

import lite_mcp_cli as cli

TOOL_SOURCE = '''from fastmcp.tools import tool

print("tool import noise")

@tool
def echo(value: str) -> str:
    """Return the supplied value."""
    print("tool invocation noise")
    return value
'''

CONTEXT_TOOL_SOURCE = '''from fastmcp.tools import tool
import lite_mcp_idempotency
from lite_mcp_artifacts import get_artifact_store

ARTIFACT_STORE = get_artifact_store()
if lite_mcp_idempotency._configured_store is None:
    raise RuntimeError("idempotency helper was not configured before tool import")

@tool
def echo() -> dict[str, str]:
    """Return configured helper paths."""
    return {
        "database": str(lite_mcp_idempotency._configured_store.database_path),
        "artifacts": str(ARTIFACT_STORE.root),
    }
'''

IDEMPOTENT_TOOL_SOURCE = '''from fastmcp.tools import tool
from lite_mcp_idempotency import run_once

@tool
def echo(value: str) -> dict[str, object]:
    """Return one durable result."""
    outcome = run_once(
        "cli.echo",
        value,
        {"value": value},
        lambda transaction: {"value": value},
    )
    return {"value": outcome.value, "replayed": outcome.replayed}
'''


class CliRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.tools = self.root / "tools"
        self.tools.mkdir()
        tool_path = self.tools / "echo.py"
        tool_path.write_text(TOOL_SOURCE, encoding="utf-8")
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "version": 1,
                    "pythonVersion": "3.14.0",
                    "toolsDir": str(self.tools),
                    "tools": [
                        {
                            "name": "echo",
                            "path": str(tool_path),
                            "sourceFilename": "echo.py",
                            "sourceSha256": hashlib.sha256(
                                TOOL_SOURCE.encode()
                            ).hexdigest(),
                            "dependencies": [],
                            "requiresPython": ">=3.14,<3.15",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.dashboard = self.root / "dashboard"
        widgets = self.dashboard / "widgets"
        widgets.mkdir(parents=True)
        (self.dashboard / "app.py").write_text("pass\n", encoding="utf-8")
        (widgets / "card.py").write_text(
            'print("widget import noise")\n\nTITLE = "Card"\n\ndef render(context):\n    pass\n',
            encoding="utf-8",
        )
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def arguments(self, mode, **overrides):
        values = {
            "mode": mode,
            "manifest": self.manifest,
            "dashboard": self.dashboard,
            "workspace": self.workspace,
            "tool": None,
            "timeout": 60.0,
            "result_limit": 4096,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def replace_tool(self, source):
        tool_path = self.tools / "echo.py"
        tool_path.write_text(source, encoding="utf-8")
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["tools"][0]["sourceSha256"] = hashlib.sha256(
            source.encode()
        ).hexdigest()
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")

    def test_check_loads_real_tools_and_widgets_without_touching_configured_database(
        self,
    ):
        configured_database = self.root / "configured.db"
        output = io.StringIO()
        noise = io.StringIO()
        with (
            patch.dict(os.environ, {"LITE_MCP_DB": str(configured_database)}),
            redirect_stdout(output),
            redirect_stderr(noise),
        ):
            exit_code = cli._check(self.arguments("check"))

        report = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["loadedToolCount"], 1)
        self.assertEqual(report["widgets"][0]["name"], "card")
        self.assertFalse(configured_database.exists())
        self.assertIn("tool import noise", noise.getvalue())
        self.assertIn("widget import noise", noise.getvalue())
        self.assertNotIn("noise", output.getvalue())

    def test_check_configures_tool_helpers_in_temporary_paths(self):
        self.replace_tool(CONTEXT_TOOL_SOURCE)
        output = io.StringIO()

        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            exit_code = cli._check(self.arguments("check"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue())["loadedToolCount"], 1)
        self.assertFalse((self.workspace / "artifacts").exists())

    def test_invoke_returns_the_actual_fastmcp_result_encoding(self):
        output = io.StringIO()
        noise = io.StringIO()
        with (
            patch.dict(os.environ, {"LITE_MCP_DB": str(self.root / "data.db")}),
            patch("sys.stdin", io.StringIO('{"value":"hello"}')),
            redirect_stdout(output),
            redirect_stderr(noise),
        ):
            exit_code = cli._invoke(self.arguments("invoke", tool="echo"))

        report = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(report["ok"])
        self.assertEqual(report["tool"], "echo")
        self.assertIn("hello", json.dumps(report["result"]))
        self.assertIn("tool import noise", noise.getvalue())
        self.assertIn("tool invocation noise", noise.getvalue())
        self.assertNotIn("noise", output.getvalue())

    def test_invoke_configures_durable_idempotency_before_tool_import(self):
        self.replace_tool(IDEMPOTENT_TOOL_SOURCE)
        database = self.root / "data.db"
        with sqlite3.connect(database) as connection:
            connection.execute(
                """
                CREATE TABLE _lite_mcp_idempotency (
                    namespace TEXT NOT NULL,
                    business_key TEXT NOT NULL,
                    input_sha256 TEXT NOT NULL CHECK (length(input_sha256) = 64),
                    result_json BLOB NOT NULL,
                    result_sha256 TEXT NOT NULL CHECK (length(result_sha256) = 64),
                    result_bytes INTEGER NOT NULL CHECK (
                        result_bytes BETWEEN 1 AND 1048576
                    ),
                    original_operation_id TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT (
                        strftime('%Y-%m-%dT%H:%M:%fZ')
                    ),
                    PRIMARY KEY (namespace, business_key)
                ) STRICT, WITHOUT ROWID
                """
            )

        reports = []
        for _ in range(2):
            output = io.StringIO()
            with (
                patch.dict(os.environ, {"LITE_MCP_DB": str(database)}),
                patch("sys.stdin", io.StringIO('{"value":"hello"}')),
                redirect_stdout(output),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(
                    cli._invoke(self.arguments("invoke", tool="echo")),
                    0,
                )
            reports.append(json.loads(output.getvalue()))

        self.assertIn('"replayed": false', json.dumps(reports[0]))
        self.assertIn('"replayed": true', json.dumps(reports[1]))


if __name__ == "__main__":
    unittest.main()
