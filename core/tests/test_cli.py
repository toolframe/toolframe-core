import hashlib
import io
import json
import os
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

    def test_check_loads_real_tools_and_widgets_without_touching_configured_database(self):
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

    def test_invoke_returns_the_actual_fastmcp_result_encoding(self):
        output = io.StringIO()
        noise = io.StringIO()
        with (
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


if __name__ == "__main__":
    unittest.main()
