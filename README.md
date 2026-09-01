# lite-mcp

`lite-mcp` is a LAN-only Streamlit dashboard and startup-only Python MCP
tool hub. A Rust launcher prepares SQLite and one shared UV environment, then
supervises one Python 3.14 process, one Uvicorn server, and one port. The
Streamlit `st.App` is mounted at `/`; FastMCP serves modern Streamable HTTP at
`/mcp`.

> [!WARNING]
> The server listens on `0.0.0.0` and has no authentication by default. The
> dashboard is trusted Python code: it can read and write `data.db`, share the
> Python process with every MCP tool, inherit its environment and secrets, and
> use the writable workspace. Dashboard results and submitted data are visible
> to anyone who can reach the LAN address. Use it only on a trusted LAN.

## Package and run

The distributable is one editable folder and one launch command—not literally
one executable—because it includes a platform-matched `uv` binary, dashboard
source, tools, workspace, and runtime directories.

The MVP targets macOS first and currently uses Unix inherited file descriptors
for race-free handoff to Uvicorn.

```sh
UV_BIN=/path/to/uv ./scripts/package.sh
./dist/lite-mcp/lite-mcp
```

The package contains the complete editable dashboard scaffold:

```text
lite-mcp/
├── lite-mcp
├── uv
├── dashboard/
│   ├── app.py
│   ├── pyproject.toml
│   ├── components/
│   └── widgets/
│       └── example_card.py
├── tools/
│   └── execute_sql.py
├── workspace/
└── runtime/
```

The packaging script refuses to overwrite an existing `dist/lite-mcp` and
copies dashboard source files without `__pycache__`, `.pyc`, or `.pyo` files.
It does not alter the source tree. At runtime, a missing entire `dashboard/`
directory is created from the embedded scaffold. Existing dashboard
directories and files are never overwritten, completed, or automatically
upgraded.

On first online launch, UV downloads Python 3.14 and the pinned core,
dashboard, and tool dependencies into `runtime/`; no system Python is
required. UV 0.12.8 or newer is required. Pass `--offline` only after those
artifacts are cached.

At startup the launcher creates `data.db`, binds the preferred or next
available port without a release/rebind race, validates the dashboard and
loads every tool, and prints its URLs:

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

Only modern Streamable HTTP is served. There is no legacy SSE endpoint,
client-facing stdio transport, hot reload service, reload API, or reload UI.

## Dashboard

The root page is the user dashboard. `dashboard/app.py` is its Python loader
and can be edited or completely replaced by a Coding Agent. The default loader
discovers direct public Python files in `dashboard/widgets/` and renders them.
Each discovered module must export a synchronous `render(context)` function.
The filename stem is the stable widget ID. A widget may optionally export a
string `TITLE` and integer `ORDER`; widgets are rendered by `ORDER`, then by
filename. Duplicate or invalid metadata is a startup error.

`dashboard/components/` is a normal Python package for reusable components and
is not automatically executed. Files whose names begin with `_`, symlinks,
nested widget directories, and non-Python files are not widget entries. Every
public widget is compiled, imported, and contract-checked at startup, even if
the custom `app.py` does not render it. Use a private name such as `_draft.py`
for an unfinished widget.

The supported agent-facing API is the public `lite_mcp_dashboard` module. It
provides `DashboardContext`, `DashboardDatabaseError`, `get_context()`, and the
widget discovery/render helpers. `DashboardContext` exposes only the stable
`db`, `workspace`, `logger`, `widget_id`, and read-only runtime summary; widget
code should not depend on FastAPI, FastMCP, or Streamlit internals.

Dashboard Python is trusted application code, not a security sandbox. A
database helper makes correct, parameterized access easy, but it cannot stop a
widget from importing `sqlite3`, reading inherited environment variables, or
affecting the shared process. Only the dashboard server-side code can access
SQLite; there is no browser API that accepts arbitrary SQL.

### Editing and reruns

Dashboard source changes are picked up by a browser refresh, Streamlit rerun,
or an interaction that triggers a rerun. Streamlit source watching remains
enabled while `runOnSave=false`, so saving a file does not silently execute it.
MCP tool source changes, dashboard dependency changes, and dependency lockfile
changes require a full process restart. The MVP has no tool or dashboard reload
function.

### Dashboard dependencies

`dashboard/pyproject.toml` is a minimal dependency manifest. It may contain
`[project]` `requires-python` and `dependencies` only; Python constraints must
accept 3.14. Build systems, UV workspaces, custom package sources, and
`[tool.uv]` configuration are not supported in this MVP.

Dashboard dependencies are merged with the core and all MCP tool dependencies
into the same UV environment. A resolution conflict is a fatal startup error
and identifies the dependency and its source. After changing dependencies,
restart the application so UV can resolve and provision the shared
environment.

The MVP pins `streamlit==1.61.0` and uses Streamlit's built-in metric, table,
and chart/data components. It does not bundle D3 or ECharts. A future widget
may use Streamlit custom components when a freer frontend presentation is
needed.

## Dashboard database helper

Widgets use the shared `data.db` through `context.db` (or the equivalent
`get_context()` result):

```python
from lite_mcp_dashboard import get_context

context = get_context()
rows = context.db.query(
    "SELECT category, amount FROM expenses WHERE category = ?",
    (category,),
)
result = context.db.execute(
    "INSERT INTO expenses(category, amount) VALUES (?, ?)",
    (category, amount),
)
```

`query(sql, params)` uses a read-only SQLite connection and returns a
`pandas.DataFrame`. `execute(sql, params)` permits one parameterized DML or
DDL statement and commits it when successful. It returns a typed result with
`affected_rows`, `last_insert_rowid`, and, for SQLite `RETURNING`, an optional
DataFrame. Multi-statement SQL, `ATTACH`, `DETACH`, extension loading, and
writable security/journal PRAGMAs are rejected. Values must be bound through
parameters; interpolating user input into SQL is not supported.

Use a transaction for several changes that must commit atomically:

```python
with context.db.transaction() as transaction:
    transaction.execute(
        "UPDATE accounts SET balance = balance - ? WHERE id = ?",
        (amount, source_id),
    )
    transaction.execute(
        "UPDATE accounts SET balance = balance + ? WHERE id = ?",
        (amount, destination_id),
    )
```

Individual writes use short transactions. The transaction context commits all
statements together or rolls them back together; nested transactions and
automatic write retries are not supported. The helper configures WAL,
foreign-key enforcement, and a bounded busy timeout for concurrent LAN
sessions.

Query results are limited to 10,000 rows and 10 MiB. A result over either limit
raises `ResultTooLarge`; it is never silently truncated. There is no automatic
query cache, so a successful write followed by rerun sees shared data. A
widget may opt into `st.cache_data` for an expensive read, but it owns cache
invalidation.

Helper failures use typed `DashboardDatabaseError` values with `code`,
`message`, `retryable`, `trace_id`, and a recovery hint. Widgets should catch
errors they expect (for example validation, constraint, conflict, or temporary
lock errors) and show a useful `st.error`. A lock or transient I/O error is
retryable; syntax, constraint, invalid input, oversized result, and policy
errors are not. Full SQL and tracebacks stay in server stderr and are never
sent to a LAN browser.

The generated `widgets/example_card.py` is a complete small CRUD reference. It
creates (without pre-seeding) a clearly named
`dashboard_example_entries` table, displays a metric, table, and built-in
chart, accepts new rows with `st.form`, edits rows with `st.data_editor`, and
deletes a selected row only after explicit confirmation. It binds all values,
refreshes after a successful commit, and displays caught widget errors. Its
updates use an `id` plus version or `updated_at` check; a stale update reports
a conflict and asks the user to refresh instead of silently overwriting
another LAN session.

## Failure behavior

The dashboard is part of the application contract. Missing dashboard files,
invalid dependency metadata, dependency resolution failures, syntax errors,
imports, loader errors, invalid widget contracts, and uncaught render/callback
exceptions fail the whole application. Startup does not execute side-effecting
widget renders, so readiness cannot prove every interactive path.

If an uncaught dashboard error occurs during a later Streamlit rerun, the
runtime logs the widget, phase, exception, trace ID, and full traceback to
stderr, emits its fatal shutdown signal, and exits the Python process
gracefully. Rust reports a non-zero failure and does not auto-restart it. Fix
the dashboard and start `lite-mcp` again.

Expected user and database errors are not fatal when the widget catches them
and renders an error state. Readiness means that dependencies, dashboard
source, widget contracts, FastAPI, FastMCP, and Streamlit lifespans have
started; it does not mean that every widget has rendered successfully.

## MCP tools

Create a direct `tools/<lower_snake_case>.py` child with standard PEP 723
metadata and exactly one standalone FastMCP `@tool`. The filename and function
name must match.

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

Restart `lite-mcp` to load added or edited files. Discovery is startup-only:

- Only direct `tools/*.py` regular files are published.
- Hidden files, `_helper.py`, symlinks, and nested directories are ignored.
  Ignored private helper modules can still be imported by tools.
- Every published file must declare `requires-python` accepting Python 3.14
  and a `dependencies` array.
- `[tool.uv]` is rejected in this shared-environment MVP.
- All dependencies resolve into one content-addressed UV project, `.venv`, and
  `uv.lock`. A conflict aborts startup.
- Import, decorator, identity, schema, integrity, or dependency failures abort
  before HTTP readiness. The launcher exits nonzero with the tool path, load
  phase, cause, traceback, and recent UV/Python diagnostics.

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

Invocation failures do not stop the app. They produce MCP `isError: true`
results with `{code, message, retryable, details, traceId}`. Unexpected
exceptions include sanitized stack locations for the agent and a full
traceback in server logs without locals or environment dumps.

### SQL reference tool

`tools/execute_sql.py` is the default coding reference and is scaffolded only
when the launcher creates a brand-new tools directory. Existing directories
and files are never overwritten.

The tool executes exactly one arbitrary SQLite statement against `LITE_MCP_DB`.
Writes, DDL, PRAGMA, and `RETURNING` are allowed. SQL input is capped at 1 MiB;
query output is capped at 100 rows. Mutations report `affected_rows` and
`last_insert_rowid`. Busy/locked and transient disk I/O errors are marked
retryable; syntax, constraint, empty, oversized, missing-environment, and
multi-statement errors are not.

## HTTP routes

| Route | Purpose |
| --- | --- |
| `GET /` | Streamlit Dashboard |
| `GET /info` | Built-in datastore and runtime diagnostics page |
| `GET /api/database` | Up to 25 preview rows per SQLite table |
| `GET /api/runtime` | Python version, loaded tools, dependencies, and runtime limits |
| `GET /healthz` | Health check |
| `/mcp` | Unauthenticated FastMCP Streamable HTTP endpoint |

The `/api/database` and `/api/runtime` routes remain available for diagnostics
and compatibility. Host and browser Origin headers are restricted to
localhost, the selected bind address, and detected local interfaces. This
reduces DNS-rebinding exposure but is not authentication or a code sandbox.

## Configuration

| Option | Environment | Default |
| --- | --- | --- |
| `--host` | `LITE_MCP_HOST` | `0.0.0.0` |
| `--port` | `LITE_MCP_PORT` | `8000` |
| `--port-search-span` | `LITE_MCP_PORT_SEARCH_SPAN` | `100` |
| `--database` | `LITE_MCP_DB` | `<executable>/data.db` |
| `--tools-dir` | `LITE_MCP_TOOLS` | `<executable>/tools` |
| `--dashboard-dir` | `LITE_MCP_DASHBOARD_DIR` | `<executable>/dashboard` |
| `--workspace-dir` | `LITE_MCP_WORKSPACE` | `<executable>/workspace` |
| `--runtime-dir` | `LITE_MCP_RUNTIME` | `<executable>/runtime` |
| `--uv-path` | `LITE_MCP_UV` | sibling `uv`, then PATH |
| `--offline` | `LITE_MCP_OFFLINE` | false |
| `--max-concurrency` | `LITE_MCP_MAX_CONCURRENCY` | 60; `0` is unlimited |
| `--tool-timeout` | `LITE_MCP_TOOL_TIMEOUT` | 60 seconds; `0` disables |
| `--result-limit` | `LITE_MCP_RESULT_LIMIT` | 4 MiB |
| `--startup-timeout` | `LITE_MCP_STARTUP_TIMEOUT` | 600 seconds |

The dashboard directory override is resolved relative to the current working
directory when it is relative, matching the other path overrides.

Timeouts are best effort. A blocking thread or native extension cannot always
be safely killed inside the shared Python process.

For local-only access:

```sh
./lite-mcp --host 127.0.0.1
```

For source development, override paths because defaults are intentionally
relative to the compiled executable:

```sh
cargo run --release -- \
  --database ./data.db \
  --tools-dir ./tools \
  --dashboard-dir ./dashboard \
  --workspace-dir ./workspace \
  --runtime-dir ./runtime \
  --uv-path /path/to/uv
```

Streamlit is configured for headless operation, disabled telemetry, CORS/XSRF
protection, `runOnSave=false`, and the launcher's Host/Origin allowlist. File
uploads and WebSocket messages are limited to 25 MiB. These settings reduce
accidental LAN exposure and resource abuse; they do not replace
authentication.

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
  --with pandas \
  --with streamlit==1.61.0 \
  --with pydantic==2.12.5 \
  --with uvicorn==0.42.0 \
  python -m unittest discover -s core/tests -v
```
