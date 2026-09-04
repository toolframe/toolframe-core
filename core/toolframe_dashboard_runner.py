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
    app_dir, app_path = _dashboard_source()
    namespace: dict[str, Any] = {
        "__name__": "__main__",
        "__file__": str(app_path),
        "__package__": None,
        "__cached__": None,
        "__spec__": None,
    }
    inserted_path = str(app_dir) not in sys.path
    if inserted_path:
        sys.path.insert(0, str(app_dir))
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
        from toolframe_runtime import report_dashboard_fatal

        report_dashboard_fatal(error)
        if isinstance(error, Exception):
            raise
        raise DashboardExecutionError(app_path, error) from error
    finally:
        if inserted_path:
            try:
                sys.path.remove(str(app_dir))
            except ValueError:
                pass


def _app_directory() -> Path:
    app_value = os.environ.get("TOOLFRAME_APP_DIR", "").strip()
    if not app_value:
        raise RuntimeError("TOOLFRAME_APP_DIR is required")
    path = Path(app_value)
    if not path.is_absolute():
        raise RuntimeError("TOOLFRAME_APP_DIR must be an absolute path")
    if not path.is_dir() or path.is_symlink():
        raise RuntimeError("TOOLFRAME_APP_DIR must name a directory, not a symlink")
    return path


def _dashboard_source() -> tuple[Path, Path]:
    directory = _app_directory()
    return directory, directory / "dashboard.py"


def _is_streamlit_control_exception(error: BaseException) -> bool:
    return any(
        base.__name__ == "ScriptControlException"
        and base.__module__.startswith("streamlit.")
        for base in type(error).__mro__
    )


if __name__ == "__main__":
    main()
