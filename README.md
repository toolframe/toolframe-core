# lite-mcp

`lite-mcp` is a startup-only Python tool hub for MCP. A small Rust launcher prepares SQLite and a shared UV environment, then supervises one Python 3.14 FastAPI/FastMCP process. It chooses an available port, serves a database/tool dashboard, and exposes Streamable HTTP at `/mcp`.

> [!WARNING]
> The server listens on `0.0.0.0` and has no authentication by default. Every loaded tool is trusted code: tools share one Python process, globals, inherited environment and secrets, the SQLite database, and the writable workspace. Use it only on a trusted LAN.

## Package and run

The distributable is one folder and one launch command—not literally one executable—because it includes a platform-matched `uv` binary and an editable `tools/` directory.

The MVP targets macOS first and currently uses Unix inherited file descriptors for race-free handoff to Uvicorn.

```sh
UV_BIN=/path/to/uv ./scripts/package.sh
./dist/lite-mcp/lite-mcp
```

The packaging script requires UV 0.12.8 or newer so UV can provision a final Python 3.14 release. On first online launch, UV downloads Python 3.14 and the pinned core/tool dependencies into `runtime/`; no system Python is required. Pass `--offline` only after those artifacts are cached.

The folder starts as:

```text
lite-mcp/
├── lite-mcp
├── uv
├── tools/
│   └── execute_sql.py
├── workspace/
└── runtime/
```

At startup the launcher creates `data.db`, binds the preferred or next available port without a release/rebind race, strictly loads every tool, and prints its URLs:

```text
lite-mcp 0.1.0 is ready
Dashboard: http://127.0.0.1:8000/
MCP:       http://127.0.0.1:8000/mcp
LAN:       http://192.168.1.20:8000/
```

Connect Codex using the printed MCP URL:

```sh
codex mcp add lite-mcp --url http://127.0.0.1:8000/mcp
```

Or configure the same URL:

```toml
[mcp_servers.lite_mcp]
url = "http://127.0.0.1:8000/mcp"
```

Only modern Streamable HTTP is served. There is no legacy SSE endpoint and no client-facing stdio transport.

## Add a Python tool

Create a direct `tools/<lower_snake_case>.py` child with standard PEP 723 metadata and exactly one standalone FastMCP `@tool`. The filename and function name must match.

```python
# /// script
# requires-python = ">=3.14,<3.15"
# dependencies = ["httpx==0.28.1"]
# ///

from fastmcp.tools import tool


@tool
def fetch_title(url: str) -> str:
    """Return the HTML title for one URL."""
    import httpx

    response = httpx.get(url, timeout=10)
    response.raise_for_status()
    return response.text.split("<title>", 1)[1].split("</title>", 1)[0]
```

Restart `lite-mcp` to load added or edited files. The MVP has no watcher, hot reload, reload API, or reload UI.

Discovery rules:

- Only direct `tools/*.py` regular files are published.
- Hidden files, `_helper.py`, symlinks, and nested directories are ignored. Ignored private helper modules can still be imported by tools.
- Every published file must declare `requires-python` accepting Python 3.14 and a `dependencies` array.
- `[tool.uv]` is rejected in this shared-environment MVP.
- All dependencies are resolved into one content-addressed UV project, `.venv`, and `uv.lock`. A conflict aborts startup.
- Import, decorator, identity, schema, integrity, or dependency failures abort before HTTP readiness. The launcher exits nonzero with the tool path, load phase, cause, traceback, and recent UV/Python diagnostics.

Tool authors can raise `LiteMcpError` from the runtime support module:

```python
from lite_mcp_runtime import LiteMcpError

raise LiteMcpError(
    "upstream_busy",
    "The upstream service is temporarily unavailable.",
    retryable=True,
    details={"recoveryHint": "Retry after a short delay."},
)
```

Invocation failures do not stop the app. They produce MCP `isError: true` results with `{code, message, retryable, details, traceId}`. Unexpected exceptions include sanitized stack locations for the agent and a full traceback in server logs without locals or environment dumps.

## SQL reference tool

`tools/execute_sql.py` is the default coding reference and is scaffolded only when the launcher creates a brand-new tools directory. Existing directories and files are never overwritten.

The tool executes exactly one arbitrary SQLite statement against `LITE_MCP_DB`. Writes, DDL, PRAGMA, and `RETURNING` are allowed. SQL input is capped at 1 MiB; query output is capped at 100 rows. Mutations report `affected_rows` and `last_insert_rowid`. Busy/locked and transient disk I/O errors are marked retryable; syntax, constraint, empty, oversized, missing-environment, and multi-statement errors are not.

## HTTP routes

| Route | Purpose |
| --- | --- |
| `GET /` | Database and startup tool dashboard |
| `GET /api/database` | Up to 25 preview rows per SQLite table |
| `GET /api/runtime` | Python version, loaded tools, dependencies, and runtime limits |
| `GET /healthz` | Health check |
| `/mcp` | Unauthenticated FastMCP Streamable HTTP endpoint |

Host and browser Origin headers are restricted to localhost, the selected bind address, and detected local interfaces. This reduces DNS-rebinding exposure but is not authentication or a sandbox.

## Configuration

| Option | Environment | Default |
| --- | --- | --- |
| `--host` | `LITE_MCP_HOST` | `0.0.0.0` |
| `--port` | `LITE_MCP_PORT` | `8000` |
| `--port-search-span` | `LITE_MCP_PORT_SEARCH_SPAN` | `100` |
| `--database` | `LITE_MCP_DB` | `<executable>/data.db` |
| `--tools-dir` | `LITE_MCP_TOOLS` | `<executable>/tools` |
| `--workspace-dir` | `LITE_MCP_WORKSPACE` | `<executable>/workspace` |
| `--runtime-dir` | `LITE_MCP_RUNTIME` | `<executable>/runtime` |
| `--uv-path` | `LITE_MCP_UV` | sibling `uv`, then PATH |
| `--offline` | `LITE_MCP_OFFLINE` | false |
| `--max-concurrency` | `LITE_MCP_MAX_CONCURRENCY` | 60; `0` is unlimited |
| `--tool-timeout` | `LITE_MCP_TOOL_TIMEOUT` | 60 seconds; `0` disables |
| `--result-limit` | `LITE_MCP_RESULT_LIMIT` | 4 MiB |
| `--startup-timeout` | `LITE_MCP_STARTUP_TIMEOUT` | 600 seconds |

Timeouts are best effort. A blocking thread or native extension cannot always be safely killed inside the shared Python process.

For local-only access:

```sh
./lite-mcp --host 127.0.0.1
```

For source development, override paths because defaults are intentionally relative to the compiled executable:

```sh
cargo run --release -- \
  --database ./data.db \
  --tools-dir ./tools \
  --workspace-dir ./workspace \
  --runtime-dir ./runtime \
  --uv-path /path/to/uv
```

## Development checks

```sh
cargo fmt --all -- --check
cargo test --all-targets
cargo clippy --all-targets -- -D warnings
cargo build --release

uv run --python 3.14 \
  --with 'anyio>=4.10,<5' \
  --with fastapi==0.135.2 \
  --with fastmcp==4.0.0 \
  --with pydantic==2.12.5 \
  --with uvicorn==0.42.0 \
  python -m unittest discover -s core/tests -v
```
