"""Launch the private lite-mcp FastAPI/FastMCP runtime."""

from __future__ import annotations

import logging
import os

import uvicorn

from lite_mcp_runtime import build_application


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LITE_MCP_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = build_application()
    listen_fd = int(os.environ["LITE_MCP_LISTEN_FD"])
    uvicorn.run(
        app,
        fd=listen_fd,
        workers=1,
        access_log=False,
        log_level=os.environ.get("LITE_MCP_UVICORN_LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
