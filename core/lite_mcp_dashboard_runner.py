"""Managed Streamlit entrypoint that executes the user-owned dashboard."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


class DashboardExecutionError(RuntimeError):
    """Turn non-Exception exits into fatal Streamlit script errors."""

    def __init__(self, path: Path, error: BaseException) -> None:
        self.path = path
        self.phase = "app-execution"
        super().__init__(f"{type(error).__name__} escaped from {path}")


def main() -> None:
    dashboard_dir = _dashboard_directory()
    app_path = dashboard_dir / "app.py"
    namespace: dict[str, Any] = {
        "__name__": "__main__",
        "__file__": str(app_path),
        "__package__": None,
        "__cached__": None,
        "__spec__": None,
    }
    inserted_path = str(dashboard_dir) not in sys.path
    if inserted_path:
        sys.path.insert(0, str(dashboard_dir))
    try:
        if not app_path.is_file() or app_path.is_symlink():
            raise RuntimeError(
                f"dashboard app must be a regular, non-symlink file: {app_path}"
            )
        source = app_path.read_bytes()
        code = compile(source, str(app_path), "exec", dont_inherit=True)
        exec(code, namespace, namespace)  # noqa: S102 - execute the trusted dashboard.
    except BaseException as error:
        if _is_streamlit_control_exception(error):
            raise
        # Report directly because Streamlit intentionally omits compile errors,
        # SystemExit, and KeyboardInterrupt from on_script_error. The callback
        # remains a backup for callback failures outside this runner boundary;
        # both paths share one first-wins reporter.
        from lite_mcp_runtime import report_dashboard_fatal

        report_dashboard_fatal(error)
        if isinstance(error, Exception):
            raise
        raise DashboardExecutionError(app_path, error) from error
    finally:
        if inserted_path:
            try:
                sys.path.remove(str(dashboard_dir))
            except ValueError:
                pass


def _dashboard_directory() -> Path:
    value = os.environ.get("LITE_MCP_DASHBOARD_DIR", "").strip()
    if not value:
        raise RuntimeError("LITE_MCP_DASHBOARD_DIR is required")
    path = Path(value)
    if not path.is_absolute():
        raise RuntimeError("LITE_MCP_DASHBOARD_DIR must be an absolute path")
    if not path.is_dir() or path.is_symlink():
        raise RuntimeError(
            "LITE_MCP_DASHBOARD_DIR must name a directory, not a symlink"
        )
    return path


def _is_streamlit_control_exception(error: BaseException) -> bool:
    return any(
        base.__name__ == "ScriptControlException"
        and base.__module__.startswith("streamlit.")
        for base in type(error).__mro__
    )


if __name__ == "__main__":
    main()
