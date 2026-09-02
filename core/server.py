"""Launch the private lite-mcp FastAPI/FastMCP runtime."""

from __future__ import annotations

import logging
import os

import uvicorn
from lite_mcp_runtime import STREAMLIT_RESOURCE_LIMIT_MIB, build_application


class CategorizedFormatter(logging.Formatter):
    """Keep child diagnostics concise and classifiable by the Rust launcher."""

    def format(self, record: logging.LogRecord) -> str:
        category = "PYTHON"
        if record.name.startswith(("fastmcp", "mcp.")):
            category = "MCP"
        elif record.name.startswith(("streamlit", "lite_mcp.dashboard")):
            category = "DASHBOARD"
        message = super().format(record)
        return f"[{category}] {record.levelname} {record.name} {message}"


def _configure_diagnostics() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(CategorizedFormatter("%(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(os.environ.get("LITE_MCP_LOG_LEVEL", "WARNING"))
    # FastMCP installs Rich handlers at import time. Remove all child-owned
    # formatting so diagnostics have one categorized stderr path.
    namespaces = ("fastmcp", "mcp", "uvicorn", "streamlit")
    names = set(namespaces)
    names.update(
        name
        for name, value in logging.Logger.manager.loggerDict.items()
        if isinstance(value, logging.Logger)
        and any(name.startswith(f"{namespace}.") for namespace in namespaces)
    )
    for name in names:
        child = logging.getLogger(name)
        child.handlers.clear()
        child.propagate = True
        child.setLevel(logging.NOTSET)


def main() -> None:
    _configure_diagnostics()
    app = build_application()
    listen_fd = int(os.environ["LITE_MCP_LISTEN_FD"])
    uvicorn.run(
        app,
        fd=listen_fd,
        workers=1,
        access_log=False,
        log_config=None,
        ws_max_size=STREAMLIT_RESOURCE_LIMIT_MIB * 1024 * 1024,
        log_level=os.environ.get("LITE_MCP_UVICORN_LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
