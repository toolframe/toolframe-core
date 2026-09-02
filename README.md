# lite-mcp

`lite-mcp` is a LAN-only Streamlit dashboard and startup-only Python MCP
tool hub. A Rust launcher applies SQLite migrations and prepares one shared UV environment, then
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
├── migrations/
│   ├── 0000_bootstrap.sql
│   └── README.md
├── tools/
│   └── execute_sql.py
├── workspace/
├── logs/                       # runtime audit and MCP JSONL files
└── runtime/
```

The packaging script refuses to overwrite an existing `dist/lite-mcp` and
copies the editable dashboard and migration source. Dashboard `__pycache__`,
`.pyc`, and `.pyo` files are excluded. It does not alter the source tree. At
runtime, a missing entire `dashboard/` or `migrations/` directory is created
from the embedded scaffold. Existing directories and files are never
overwritten, completed, or automatically upgraded.

On first online launch, UV downloads Python 3.14 and the pinned core,
dashboard, and tool dependencies into `runtime/`; no system Python is
required. UV 0.12.8 or newer is required. Pass `--offline` only after those
artifacts are cached.

At startup the launcher validates and applies migrations to `data.db`, then
binds the preferred or next available port without a release/rebind race,
validates the dashboard, loads every tool, and prints categorized diagnostics
to stderr. A typical terminal shows:

```text
07:21:48.755 [BOOT] lite-mcp 0.1.0 is ready
07:21:48.755 [DASHBOARD] Dashboard  http://127.0.0.1:8000/ (Streamlit)
07:21:48.755 [MCP] MCP        http://127.0.0.1:8000/mcp
07:21:48.755 [DB] Migrations /path/to/lite-mcp/migrations (current 0000; 1 applied this start; 1 total)
07:21:48.755 [NETWORK] LAN        http://192.168.1.20:8000/
07:21:48.755 [SECURITY] WARNING no authentication: LAN clients can read and mutate data and call trusted tools
```

Human diagnostics use local `HH:MM:SS.mmm` timestamps and are colorized only
when stderr is a TTY. `--no-color`, `LITE_MCP_NO_COLOR`, or the standard
`NO_COLOR` environment variable disables ANSI colors. Routine UV and Python
dependency output is summarized; use `--verbose` or `LITE_MCP_VERBOSE` for the
full child output and debug diagnostics. stdout is reserved for the two
machine-readable assignments `LITE_MCP_URL=...` and `LITE_MCP_MCP_URL=...`.

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

## Coding-agent development CLI

Running `lite-mcp` without a subcommand still starts the server. `lite-mcp
serve` is the explicit equivalent. The same binary also provides bounded local
commands for coding agents; path and runtime options may appear before or after
the subcommand.

### Preflight validation

Use `check` before starting the server or after editing migrations, tools,
dashboard dependencies, or widgets:

```sh
./lite-mcp check
./lite-mcp check --format json
```

`check` exercises the production migration engine against an isolated SQLite
snapshot, resolves the real shared UV project, strictly imports every startup
tool, materializes its FastMCP schema, compiles `dashboard/app.py`, and imports
and validates every public widget. The JSON form writes a versioned object to
stdout and emits JSON on failure, making it suitable for coding-agent gates.

Preflight does not bind a port, mutate the configured `data.db` or its SQLite
sidecars, create logs, or scaffold missing source directories. If the database
exists, `data.db` and any existing `-wal`/`-shm` files are copied before SQLite
opens the temporary snapshot; hashes and the source file set are checked before
and after the copy. Stop the server and other database writers first: a change
during copying fails preflight and asks the caller to retry. A missing database
uses a new in-memory database. Tool imports receive a separate temporary
`LITE_MCP_DB` path. Check may provision Python and dependencies and update
managed content under `runtime/`; imported tools and widgets remain trusted
Python code and can perform other import-time side effects. Their stdout is
forwarded to stderr so stdout remains one JSON document. The report includes
the exact paths, Python and UV versions, migration range and pending count,
tools, widgets, and merged dependencies.

### Local tool invocation

`invoke` loads the complete startup catalog into one bounded Python 3.14
process in the same shared UV environment, calls one tool through FastMCP, and
prints one JSON result:

```sh
./lite-mcp invoke fetch_title --json '{"url":"https://example.com"}'
./lite-mcp invoke execute_sql --json '{"sql":"SELECT 1 AS ready"}'
```

Arguments must be one JSON object. A successful call exits `0`; an MCP
`isError` result or runner failure exits nonzero while preserving a
machine-readable JSON response when one is available. Invocation starts no
HTTP server and performs no hot reload. It strictly loads all tools once, does
not apply migrations automatically, and does not write the server audit logs.
The invoked tool retains its normal trusted capabilities, so a tool such as
`execute_sql` can intentionally mutate the configured database. Tool import
and invocation prints are forwarded to stderr; stdout remains the single JSON
result.

### Safe scaffolding

Each scaffold command creates exactly one direct source file and refuses to
overwrite an existing path:

```sh
./lite-mcp scaffold migration add_status
./lite-mcp scaffold tool fetch_status
./lite-mcp scaffold widget status_card
```

Migration scaffolding validates the existing contiguous history and chooses
the next four-digit version. Tool and widget names use lowercase snake case
and must not be Python keywords. The target `migrations/`, `tools/`, or
`dashboard/widgets/` directory must already exist; scaffolding never creates,
repairs, or replaces a user-owned directory.

## Database migrations

`migrations/` is the source of truth for the `data.db` schema and includes an
agent-facing [authoring reference](migrations/README.md). The default
`0000_bootstrap.sql` creates the table used by the example dashboard widget.
The launcher also creates and validates its private migration ledger and
idempotency receipt table as runtime-owned internal schema. These startup
steps happen before it binds the HTTP listener or starts Python, so a migration
or incompatible internal schema fails the whole application.

To change the schema, add the next direct migration file and update the widget
or tool that uses it in the same code change:

```sql
-- migrations/0001_add_status.sql
ALTER TABLE dashboard_example_entries
ADD COLUMN status TEXT NOT NULL DEFAULT 'open';

CREATE INDEX dashboard_example_entries_status_idx
ON dashboard_example_entries(status);
```

Files must form one contiguous sequence starting with the exact
`0000_bootstrap.sql`, using names such as `0001_add_status.sql`. Descriptions
may contain lowercase ASCII letters, digits, `_`, and `-`. Only direct,
regular, non-symlink UTF-8 `.sql` files are migrations, and each file is
limited to 4 MiB. Hidden/private files, non-SQL files, and nested files are
ignored.

All pending files run in numeric order inside one `BEGIN IMMEDIATE`
transaction. A failure rolls back the complete pending batch. The internal
`_lite_mcp_migrations` table records each filename and exact-byte SHA-256
checksum. Never edit, rename, or delete an applied migration: restore the
original file and add the next migration instead.

Migration SQL may contain multiple schema or data statements. It must not
contain `BEGIN`, `COMMIT`, `ROLLBACK`, `SAVEPOINT`, `RELEASE`, `PRAGMA`,
`ATTACH`, `DETACH`, or `CREATE TRIGGER`; the launcher owns the transaction,
SQLite configuration, and internal table protection. Migrations run only at
process startup, so restart `lite-mcp` after adding one. Back up `data.db`
before destructive migrations.

If the entire migrations directory is absent, the launcher creates the
bootstrap and reference without overwriting anything. An existing empty or
incomplete directory is user-owned and fails validation rather than being
silently repaired. Runtime-owned internal tables do not consume user migration
numbers and are created or validated for both new and existing databases.

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

The supported agent-facing APIs are the public `lite_mcp_dashboard` and
`lite_mcp_idempotency` modules. They provide `DashboardContext`, database and
idempotency errors/results, `get_context()`, `run_once()`, and the widget
discovery/render helpers. `DashboardContext` exposes only the stable `db`,
`workspace`, `logger`, `widget_id`, and read-only runtime summary; widget code
should not depend on FastAPI, FastMCP, or Streamlit internals.

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
Use `execute` for runtime interaction; durable schema changes belong in the
versioned migration directory.

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

### Explicit durable idempotency

Use `context.db.run_once(namespace, key, input, mutation)` when a dashboard
action may be repeated after a browser, MCP, or network retry. The caller owns
the business key; the callback receives the only transaction in which its
business writes may run:

```python
def create_expense(transaction):
    cursor = transaction.execute(
        "INSERT INTO expenses(category, amount) VALUES (?, ?)",
        (category, amount),
    )
    return {"expense_id": cursor.lastrowid, "category": category, "amount": amount}

outcome = context.db.run_once(
    "expenses.create",
    request_id,
    {"category": category, "amount": amount},
    create_expense,
)
expense = outcome.value
```

MCP tools use the same contract via
`from lite_mcp_idempotency import run_once`; see the complete
[`examples/idempotent_create_entry.py`](examples/idempotent_create_entry.py)
reference. `run_once` returns `IdempotencyResult(value, replayed,
operation_id, original_operation_id)`. Both the first caller and replays get
the value decoded from the durable JSON bytes, so their value shape is the
same.

Namespaces are 1–128 bytes of lowercase ASCII letters, digits, `.`, `_`, `:`,
or `-`; keys are caller-chosen, trimmed UTF-8 strings up to 512 bytes. Input is
canonical JSON and its SHA-256 fingerprint is stored with the key. Reusing the
same namespace/key with different input raises nonretryable
`idempotency_key_mismatch`; it never runs the new callback. Input and result
JSON are each capped at 1 MiB, must use string object keys and finite numbers,
and cannot contain custom Python objects or binary values.

The helper first checks for an existing result, then uses `BEGIN IMMEDIATE`
and checks again before running a new callback. Concurrent first calls for the
same key therefore produce one mutation and one replay. SQLite still has one
writer, so unrelated new mutations may wait up to the five-second busy
timeout. The helper does not retry arbitrary SQL.

The business writes and successful result receipt commit in the same SQLite
transaction. Callback exceptions, invalid/oversized results, and commit
failures roll back both and leave the key unclaimed. A crash before commit
does the same; a retry after a commit replays the stored result. Do not call
`run_once` inside `context.db.transaction()` or nest it in another `run_once`;
put the complete business mutation in its callback instead.

The launcher owns `_lite_mcp_idempotency` and validates its exact schema at
every startup. User migrations and `run_once` callback SQL cannot read, alter,
or write that table; only the helper inserts receipts after the business
callback succeeds. Configure result limits at or below the table's fixed
1 MiB durable ceiling.

`IdempotencyError` has `code`, `message`, `retryable`, `trace_id`, `details`,
and a recovery hint. MCP middleware converts it to the normal structured tool
error contract. Busy and transient storage failures are retryable; key/input
mismatches, validation failures, result limits, constraints, and corrupt
stored receipts are not. Audit records include start/completion, replay
status, the idempotency transaction ID, and the current MCP operation or
dashboard request correlation fields.

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
errors are not. Full SQL and tracebacks stay server-side in stderr and the
structured logs; they are never sent to a LAN browser. Dashboard mutation
records are written to the audit log described below; direct `sqlite3` access
remains an unlogged trusted-code escape hatch.

The generated `widgets/example_card.py` is a complete small CRUD reference. It
uses the `dashboard_example_entries` table owned by `0000_bootstrap.sql`,
displays a metric, table, and built-in chart, accepts new rows with `st.form`,
edits rows with `st.data_editor`, and deletes a selected row only after explicit
confirmation. It binds all values, refreshes after a successful commit, and
displays caught widget errors. Its updates use an `id` plus version or
`updated_at` check; a stale update reports a conflict and asks the user to
refresh instead of silently overwriting another LAN session.

## Structured logging and audit

The runtime writes two synchronous UTF-8 JSONL streams under `logs/`, one file
per UTC date:

```text
logs/
├── audit-YYYY-MM-DD.jsonl
└── mcp-YYYY-MM-DD.jsonl
```

`mcp-*` contains valid MCP transport and protocol messages in both directions,
including server-to-client wire responses, tool calls, notifications,
arguments, and normalized results. `audit-*`
contains dashboard mutations and transaction events, malformed or rejected
MCP requests, Host/Origin rejections, retention and tail recovery, and Python
runtime start/stop/fatal events. Rust migration, UV, and bootstrap diagnostics
remain console-only. A process-exclusive `logs/.lite-mcp.lock` prevents two
instances from writing the same directory.

Every record uses schema version 1, a UTC RFC3339 timestamp, a process
`instanceId`, and a process-wide increasing `sequence` shared by both writers.
Correlation fields include `operationId`, the protocol request ID,
`transactionId`, `dashboardSessionId`, `requestId`, and error/trace IDs where
available. MCP records include the socket peer IP/port, self-declared client
name/version, wire session ID, method, protocol headers, and user agent; the
client metadata is not authenticated and forwarded headers are ignored.

Arguments, parameters, request bodies, and results larger than 16 KiB are
represented by a head-and-tail preview with a truncation marker, original byte
count, and SHA-256. Tracebacks have an independent 64 KiB limit with the same
metadata. JSON-compatible values are preserved; binary or non-UTF-8 data is
base64-encoded. There is no additional redaction, so SQL, parameters,
payloads, results, and secrets may appear in these files. Treat `logs/` as
sensitive and restrict access to trusted operators.

Dashboard audit coverage is guaranteed for `context.db.execute()`,
`context.db.transaction()`, and `context.db.run_once()`: mutation
starts/completions, rejected mutations, idempotent creation/replay/failure,
transaction begin/commit/rollback, and regular transaction statements are
recorded.
Standalone reads are not audited. A widget that opens `sqlite3` directly
bypasses this audit contract. JSONL and SQLite cannot be committed atomically;
a database commit can therefore be followed by a missing completion record if
the writer fails.

The runtime keeps the configured number of UTC date buckets including today
(14 by default), deletes old `audit`/`mcp` files as date pairs, and enforces a
combined 256 MiB cap by default. Unknown files are preserved. A partial final
line is trimmed to the last complete newline at startup and recorded as a
recovery event. There is no log viewer or web log API.

Logging is fail-closed: setup, serialization, append, flush, rollover,
retention, or close failures stop the Python runtime and the launcher exits
non-zero. If a current operation cannot be recorded, it is not reported as a
successful operation; the terminal receives the failure detail, including the
writer path and OS error when applicable.

## Failure behavior

Migration discovery, integrity, metadata, or SQL failures are fatal before the
listener is bound or Python starts. Diagnostics identify the migration phase,
path, version and checksum when available, SQLite code, retryability, and a
recovery hint. A busy/locked or transient I/O failure may be retryable; an
invalid name, history drift, incompatible ledger, syntax error, or constraint
failure is not.

The dashboard is part of the application contract. Missing dashboard files,
invalid dependency metadata, dependency resolution failures, syntax errors,
imports, loader errors, invalid widget contracts, and uncaught render/callback
exceptions fail the whole application. Startup does not execute side-effecting
widget renders, so readiness cannot prove every interactive path.

Structured-log setup and runtime writer failures are also fatal. The launcher
reports the underlying error and any applicable writer path, terminates the
Python process, and exits non-zero. The best-effort MCP error response may be
unavailable if a response has already started; the reliable contract is
connection termination and process failure.

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

For business mutations, prefer the explicit `lite_mcp_idempotency.run_once`
helper documented above. It protects a caller-chosen operation from MCP
transport retries without applying an automatic retry policy to arbitrary
tool SQL.

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

This tool is for ad hoc inspection and repair, not schema history. A durable
schema change must be a new `migrations/NNNN_description.sql` file so every
database receives the same ordered change and drift remains detectable.

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
| `--migrations-dir` | `LITE_MCP_MIGRATIONS_DIR` | `<executable>/migrations` |
| `--tools-dir` | `LITE_MCP_TOOLS` | `<executable>/tools` |
| `--dashboard-dir` | `LITE_MCP_DASHBOARD_DIR` | `<executable>/dashboard` |
| `--workspace-dir` | `LITE_MCP_WORKSPACE` | `<executable>/workspace` |
| `--runtime-dir` | `LITE_MCP_RUNTIME` | `<executable>/runtime` |
| `--logs-dir` | `LITE_MCP_LOGS_DIR` | `<executable>/logs` |
| `--log-retention-days` | `LITE_MCP_LOG_RETENTION_DAYS` | `14` UTC date buckets |
| `--log-max-total-mib` | `LITE_MCP_LOG_MAX_TOTAL_MIB` | `256` MiB combined |
| `--verbose` | `LITE_MCP_VERBOSE` | false |
| `--no-color` | `LITE_MCP_NO_COLOR` | false; `NO_COLOR` also disables ANSI |
| `--uv-path` | `LITE_MCP_UV` | sibling `uv`, then PATH |
| `--offline` | `LITE_MCP_OFFLINE` | false |
| `--max-concurrency` | `LITE_MCP_MAX_CONCURRENCY` | 60; `0` is unlimited |
| `--tool-timeout` | `LITE_MCP_TOOL_TIMEOUT` | 60 seconds; `0` disables |
| `--result-limit` | `LITE_MCP_RESULT_LIMIT` | 4 MiB |
| `--startup-timeout` | `LITE_MCP_STARTUP_TIMEOUT` | 600 seconds |

Migration, dashboard, and other explicit path overrides are resolved relative
to the current working directory when they are relative. The exception is
`--logs-dir`: a relative logs path is resolved from the executable root so
structured logs remain with the lite-mcp instance.

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
  --migrations-dir ./migrations \
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
