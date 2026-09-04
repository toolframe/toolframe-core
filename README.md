# Toolframe

Toolframe is a local-first runtime for small, agent-built business apps. One
Rust launcher runs a Streamlit dashboard, a FastMCP Streamable HTTP endpoint,
and one SQLite database on a trusted LAN.

The editable source is organized by business domain. Toolframe owns discovery,
validation, migrations, dependency setup, and process lifecycle; the master
development agent owns the business model and source.

> Toolframe has no authentication or Python sandbox. Anyone who can reach the
> LAN service can use the dashboard and published MCP tools. Run only trusted
> code and do not expose it to the public Internet.

## Editable source contract

There is no legacy root `dashboard/`, `tools/`, `migrations/`, or `llms/`
source contract. The only source-root overrides are `--app-dir` and
`--domains-dir`.

## How loading works

Startup is intentionally fail-closed:

1. Resolve `app/`, `domains/`, the database, workspace, and private runtime.
2. On `serve` only, create a complete source scaffold when an entire source
   root is absent.
3. Discover every direct public domain and require its root `__init__.py` plus
   `llms`, `migrations`, `model`, `tools`, and `ui` directories.
4. Validate the single global migration history across every domain, verify
   applied checksums, and apply pending migrations transactionally.
5. Discover every direct public `domains/*/tools/*.py` file, validate its PEP
   723 metadata, merge dependencies, and build an immutable startup manifest.
6. Prepare the pinned UV/Python 3.14 environment from `app/pyproject.toml` and
   public tool dependencies.
7. Validate `app/llms.txt`, only the domain documents it explicitly links,
   `app/dashboard.py`, every public domain model file, public tool, and public
   widget. Domain import boundaries are checked before tool import.
8. Import every public tool, materialize every MCP schema, then import and
   validate every public widget.
9. Enter the server lifespans and report readiness only after the complete
   catalog and dashboard preflight succeed.

Any broken public migration, dependency, tool, widget, domain API, or required
app file stops startup. Toolframe never serves a partial tool catalog or skips a
broken domain. The terminal error identifies the failing path and phase so the
master development agent can fix the source.

`toolframe check --format json` follows the same discovery and Python loading
path without binding a port or applying pending migrations to the configured
database. Migration validation uses isolated state; trusted Python imports can
still have import-time side effects.

## Start and inspect

```sh
./toolframe check --format json
./toolframe
```

`./toolframe serve` is the explicit form of the second command. The launcher
prints loopback URLs for this computer and one preferred LAN URL for another
computer. Give a peer agent the LAN URL ending in `/llms.txt`; give an MCP
client the same origin ending in `/mcp`.

Useful development commands:

```sh
./toolframe scaffold domain order_fulfillment
./toolframe scaffold migration order_fulfillment add_delivery_window
./toolframe scaffold tool order_fulfillment order_fulfillment_list_due_orders
./toolframe scaffold widget order_fulfillment due_orders
./toolframe invoke entry_register_list_entries --json '{"limit": 20}'
```

Names must be lower snake case and cannot be Python keywords. Scaffolding
refuses to overwrite an existing path. A tool, widget, or migration scaffold
requires an existing complete domain.

## Domain boundaries

Keep one business capability cohesive inside one domain. Domain-owned code may
import its own internals directly or relatively. Code in one domain may reach
another domain only through an explicit high-level function exported by the
other domain's root `__init__.py`:

```python
from domains.order_fulfillment import promise_delivery
```

Deep cross-domain imports such as
`from domains.order_fulfillment.model.orders import ...`, package imports,
wildcards, private names, and imports of another domain's `model`, `tools`,
`ui`, `llms`, or `migrations` package fail validation. Export a high-level
business function only when the dependency is genuinely needed; do not turn
`__init__.py` into a convenience barrel.

`app/dashboard.py` is the exception: it is the application composition root
and Toolframe does not restrict how it composes domain UI. The default file
uses the registry, but the master development agent may replace that behavior.

## Migrations

Migration files live under the domain that owns the schema, but all domains
share one immutable, contiguous numeric history:

```text
domains/entry_register/migrations/0000_bootstrap.sql
domains/entry_register/migrations/0001_adopt_domain_table_name.sql
domains/order_fulfillment/migrations/0002_add_orders.sql
```

The four-digit version is global, not per domain. Duplicate versions, gaps,
invalid names, symlinks, non-UTF-8 files, changed applied bytes, and missing
applied history are fatal. Never edit, rename, move, or delete an applied
migration. Add the next global migration instead.

Business table names use their owning domain prefix, such as
`entry_register_entries` or `order_fulfillment_orders`. `_toolframe_*` tables
belong to the runtime and are outside business migrations and repair code.

Migrations must not contain transaction control, connection PRAGMAs,
`ATTACH`/`DETACH`, or trigger creation. The launcher owns the transaction and
migration ledger. Back up `data/data.db` and its `-wal`/`-shm` sidecars before a
destructive data change.

## MCP tools

Every direct public `domains/<domain>/tools/<name>.py` file publishes exactly
one synchronous FastMCP `@tool` function. The filename, Python function name,
and MCP tool name must be identical lower snake case, and names are globally
unique across domains. Dot-prefixed files, `_*.py`, and nested directories are
not published.

Each public file begins with PEP 723 metadata:

```python
# /// script
# requires-python = ">=3.14,<3.15"
# dependencies = []
# ///

from fastmcp.tools import tool


@tool
def order_fulfillment_list_due_orders(limit: int = 50) -> dict:
    """List the bounded set of due orders needed for the next decision."""
    ...
```

Publish business commands and queries, not table CRUD, arbitrary filters, or
generic SQL. One tool should complete one guarded intent or answer one bounded
business question. Useful runtime helpers are:

- `toolframe_db.get_database()` for bounded parameterized queries and guarded
  data DML;
- `toolframe_idempotency.run_once()` for replay-safe mutations;
- `toolframe_artifacts.get_artifact_store()` for create-once large outputs; and
- `toolframe_runtime.ToolframeError` for structured, actionable MCP errors.

Do not open SQLite directly from public tools and never expose schema-changing
or repair capabilities to peer agents. The bundled
`domains/entry_register/tools/_execute_sql_reference.py` is private reference
code only; Toolframe does not publish it. A missing business operation is a
request for the master development agent to implement a narrow tool.

Tool files and dependencies are startup-loaded. Stop and restart Toolframe
after changing them.

## Dashboard and widgets

Every direct public `domains/<domain>/ui/<widget>.py` file is automatically
loaded. A widget exports synchronous `render(context)` and may define `TITLE`
and globally unique integer `ORDER` values. Dot-prefixed files, `_*.py`, and
nested directories are not widgets.

The default `app/dashboard.py` calls `render_widgets()`. It remains ordinary,
trusted Streamlit code: the master agent may select, group, reorder, or render
widgets manually, or replace the registry entirely.

Domain model code belongs in `domains/<domain>/model/` so the UI and tools can
share validation and business behavior without importing each other. Use
parameterized database helpers from the widget context; do not accept arbitrary
SQL from the browser.

Expected, recoverable interaction errors may be rendered through the one
core-owned `render_error_card()` helper. It produces one clear card containing
the message, recovery action, trace ID, and instruction to copy the error to the
master development agent. Do not create competing shared or domain-specific
error-card components. Syntax, import, contract, and other programming defects
must propagate and make the app unhealthy.

Streamlit may rerun dashboard code during browser interaction. Tool and
migration changes still require a full process restart.

## Agent documents

The three document roles are deliberately different:

Toolframe serves `app/llms.txt` at `/llms.txt` as authored; it does not generate
or append a tool catalog. The file may link focused documents using exact
root-relative paths of the form `/llms/<domain>/<name>.md`. Only those linked
regular Markdown files under `domains/<domain>/llms/` are validated and served.
Unlinked files are private and unavailable over HTTP. Changing the linked set
requires a restart; editing the bytes of an already approved index or document
is visible on the next request if the allowlist stays unchanged.

## HTTP surface

| Route | Purpose |
| --- | --- |
| `/` | Streamlit business dashboard |
| `/info` | Built-in database and startup-catalog diagnostics |
| `/healthz` | Process readiness only |
| `/api/database` | Bounded built-in table preview |
| `/api/runtime` | Immutable startup summary |
| `/llms.txt` | Authored peer-agent entry point |
| `/llms/<domain>/<document>.md` | Explicitly linked domain document |
| `/mcp` | FastMCP Streamable HTTP endpoint |

There is no SSE, stdio, reload, repair, or schema-management endpoint.

## Data, artifacts, and audit

The default database is `data/data.db`. Dashboard and tool adapters reject
runtime-owned table writes and enforce bounded results. Large durable outputs
belong under `workspace/artifacts/`; use the managed artifact API so writes are
create-once and integrity-addressed.

Structured logs are written to `.toolframe/logs/` by default:

- `audit-YYYY-MM-DD.jsonl` records application lifecycle and mutations;
- `mcp-YYYY-MM-DD.jsonl` records MCP traffic and tool results.

Logs can contain business input, output, and secrets. Default retention is 14
UTC date buckets with a combined 256 MiB cap. Logging failures are fail-closed.

## Configuration

Relative CLI path overrides resolve from the current working directory except
the logs path, which resolves from the executable root.

| CLI flag | Environment | Default beside executable |
| --- | --- | --- |
| `--database` | `TOOLFRAME_DB` | `data/data.db` |
| `--app-dir` | `TOOLFRAME_APP_DIR` | `app` |
| `--domains-dir` | `TOOLFRAME_DOMAINS_DIR` | `domains` |
| `--workspace-dir` | `TOOLFRAME_WORKSPACE` | `workspace` |
| `--runtime-dir` | `TOOLFRAME_RUNTIME` | `.toolframe` |
| `--logs-dir` | `TOOLFRAME_LOGS_DIR` | `.toolframe/logs` |

Run `./toolframe --help` for networking, UV/offline, concurrency, timeout,
result-size, and logging options. The retired per-feature source flags are not
accepted.

## Diagnose safely

Start with:

```sh
./toolframe check --format json
./toolframe --help
```

Preserve the complete terminal error and relevant JSONL logs. Do not delete the
database, runtime, domains, or migration history as a first response. Fix the
named source through the master development agent, rerun `check`, then restart
and prove the dashboard and MCP path.

Retry only errors explicitly marked retryable. Syntax, dependency, migration,
checksum, import-boundary, schema, widget, and business-rule errors require a
source or input correction.

## Build and verify this repository

```sh
cargo fmt --all -- --check
cargo clippy --all-targets -- -D warnings
cargo test --all-targets
cargo build --release
```

Create distributable binaries with `make package` for the current host or
`make release` for the supported macOS targets. The launcher embeds the default
app/domain scaffold, managed Python runtime modules, information page, and
production business-app skill.
