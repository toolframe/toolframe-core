"""Private no-server development runner for lite-mcp CLI commands."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import anyio
from fastmcp import FastMCP
from lite_mcp_artifacts import (
    DEFAULT_MAX_ARTIFACT_BYTES,
    configure_artifact_store,
)
from lite_mcp_idempotency import _configure as _configure_idempotency
from lite_mcp_runtime import RuntimeGuardMiddleware, _pydantic_json, load_tools


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("check", "invoke"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--dashboard", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--tool")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--result-limit", type=int, default=4_194_304)
    return parser


def _emit(value: Any) -> None:
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            default=_pydantic_json,
        ),
        flush=True,
    )


def _require_python() -> None:
    if sys.version_info[:2] != (3, 14) or sys.version_info.releaselevel != "final":
        raise RuntimeError(
            "lite-mcp requires a final Python 3.14 release; "
            f"found {sys.version.split()[0]} ({sys.version_info.releaselevel})"
        )


def _configure_tool_support(database: Path, workspace: Path) -> Any:
    """Configure startup-loaded tool helpers before importing user tools."""

    _configure_idempotency(database)
    return configure_artifact_store(
        workspace,
        max_artifact_bytes=DEFAULT_MAX_ARTIFACT_BYTES,
        logger=logging.getLogger("lite_mcp.artifacts"),
    )


def _check(arguments: argparse.Namespace) -> int:
    _require_python()
    with tempfile.TemporaryDirectory(prefix="lite-mcp-check-") as temporary:
        temporary_root = Path(temporary)
        database = temporary_root / "data.db"
        workspace = temporary_root / "workspace"
        workspace.mkdir()
        os.environ["LITE_MCP_DB"] = os.fspath(database)
        os.environ["LITE_MCP_WORKSPACE"] = os.fspath(workspace)
        with redirect_stdout(sys.stderr):
            artifact_store = _configure_tool_support(database, workspace)
            tools, details, _ = load_tools(arguments.manifest)

            import lite_mcp_dashboard as dashboard_sdk

            dashboard_sdk._configure(
                database_path=database,
                dashboard_dir=arguments.dashboard,
                workspace=workspace,
                logger=logging.getLogger("lite_mcp.check"),
                runtime={"startupOnly": True, "preflight": True},
                artifacts=artifact_store,
            )
            widgets = dashboard_sdk.preflight_dashboard(arguments.dashboard)
        _emit(
            {
                "pythonVersion": sys.version.split()[0],
                "tools": details,
                "widgets": [
                    {
                        "name": widget.widget_id,
                        "title": widget.title,
                        "sourceFile": widget.path.name,
                    }
                    for widget in widgets
                ],
                "loadedToolCount": len(tools),
            }
        )
    return 0


def _invoke(arguments: argparse.Namespace) -> int:
    _require_python()
    if not arguments.tool:
        raise RuntimeError("--tool is required for invoke")
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        raise ValueError("tool arguments must be a JSON object")
    with redirect_stdout(sys.stderr):
        database = Path(os.environ["LITE_MCP_DB"])
        _configure_tool_support(database, arguments.workspace)
        tools, _, _ = load_tools(arguments.manifest)

        async def invoke() -> Any:
            mcp = FastMCP("lite-mcp-local-invoke", strict_input_validation=True)
            for tool_object in tools:
                mcp.add_tool(tool_object)
            mcp.add_middleware(
                RuntimeGuardMiddleware(1, arguments.timeout, arguments.result_limit)
            )
            return await mcp.call_tool(arguments.tool, payload)

        result = anyio.run(invoke)
    _emit(
        {
            "ok": not result.is_error,
            "tool": arguments.tool,
            "result": result.to_mcp_result(),
        }
    )
    return 1 if result.is_error else 0


def main() -> int:
    arguments = _parser().parse_args()
    return _check(arguments) if arguments.mode == "check" else _invoke(arguments)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as error:
        _emit(
            {
                "ok": False,
                "error": {
                    "type": type(error).__name__,
                    "message": str(error),
                },
            }
        )
        raise SystemExit(2) from None
