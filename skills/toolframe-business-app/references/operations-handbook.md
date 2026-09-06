# Operations handbook

This handbook covers running, connecting, backing up, and diagnosing a
Toolframe business app. It is for the master development agent or trusted local
operator. A peer agent receives only `/llms.txt`, linked domain guidance, and
published MCP business tools.

## What the app is

One Rust launcher runs one managed Python 3.14 process containing:

- a Streamlit dashboard at `/`;
- built-in diagnostics at `/info`;
- a FastMCP Streamable HTTP endpoint at `/mcp`;
- the authored peer guide at `/llms.txt`; and
- one selected database: SQLite at `data/data.db` by default, or PostgreSQL.

Read [plugins-and-data.md](plugins-and-data.md) for provider settings, target
switches, schema history and plugin lifecycle.

The app has no authentication, authorization, or Python sandbox. Any reachable
LAN client can use exposed dashboard actions and MCP tools. Do not publish the
port to the Internet or run untrusted source.

## Start and connect

From the packaged folder:

```sh
./toolframe
```

`./toolframe serve` is equivalent. The first online run downloads the pinned
UV build, managed Python, and dependencies into `.toolframe/`. `--offline`
works only after those artifacts are cached. Use `./toolframe --help` for the
current platform and configuration contract.

Startup prints loopback URLs for the host computer and one preferred LAN base
URL. `127.0.0.1` is not reachable from another computer. Give a human the
printed LAN base URL and give a new peer agent that URL ending in `/llms.txt`.

The peer guide should tell the agent to register the same origin ending in
`/mcp` as Streamable HTTP through its own MCP client, then list tools. Reading
the guide is not installation; only a successful tool-list response proves the
connection.

There is no SSE, stdio, reload, repair, generic SQL, or schema-management
endpoint.

## Normal owner loop

1. Run `./toolframe check --format json` after source changes.
2. Start Toolframe and wait for the ready announcement.
3. Open the dashboard and complete the human actions.
4. Give the peer the tested LAN `/llms.txt` URL.
5. The peer registers `/mcp`, lists tools, and uses only the narrow capability
   needed for its assigned workflow.
6. Review the durable result/history in the dashboard.

If the peer discovers a missing operation or an app defect, it stops, preserves
the exact error/trace ID, and asks the master development agent for a source
change. It must not attempt schema or application repair itself.

## When a peer cannot reach MCP

Connection refused, timeout, DNS failure, an unusable route, or a stopped server
means the peer does not know current business state. It must not claim that a
business mutation succeeded or failed.

Return:

1. a visible **[app name] MCP unavailable — workflow paused** warning that says
   which reads/writes did not happen; and
2. one complete copyable issue created from
   `assets/MCP_UNAVAILABLE_ISSUE.template.md` for the master agent.

Use observed values or `Unknown`; preserve the URL, transport, client, time,
and exact error. Distinguish “this peer could not reach the route” from proof
that the server process stopped. If MCP returned a structured business error,
follow its retryability/recovery guidance instead of the unavailable template.

## What changes require a restart

- Migrations are discovered and applied at startup.
- Public domain tools and their PEP 723 dependencies are loaded at startup.
- `app/pyproject.toml` dependencies are loaded at startup.
- Domain APIs and public widgets are validated at startup. Streamlit can rerun
  dashboard code during browser interaction, but that is not a tool reload.
- The set of domain LLMS documents linked by `app/llms.txt` is fixed at startup.
  Editing approved bytes is visible on the next request; adding/removing a link
  or target requires restart.

Tool discovery considers direct public
`domains/<domain>/tools/<name>.py` files. Widget discovery considers direct
public `domains/<domain>/ui/<name>.py` files. Dot files, `_*.py`, and nested
directories are not published. Toolframe does not silently skip a broken public
file or domain.

Use the safe create-once scaffolds:

```sh
./toolframe scaffold domain <domain>
./toolframe scaffold migration <domain> <description>
./toolframe scaffold tool <domain> <name>
./toolframe scaffold widget <domain> <name>
```

## Check before restart

```sh
./toolframe check
./toolframe check --format json
```

Check validates the complete domain layout, schema/history contracts,
dependency resolution, public Python compile
and import boundaries, every public tool/schema, every public widget, and the
linked LLMS allowlist. It does not bind a port or apply pending migrations to
the configured database. SQLite uses a filesystem copy for a trial; PostgreSQL
validates history and the plan without trial DDL. The JSON report states the level.
Trusted imports must remain free of side effects.

A failed check is a release blocker. Do not hide the file, weaken validation,
or run with a partial domain.

## Back up safely

Stop the process before copying durable state. Preserve:

- the selected database (for SQLite, its file and present `-wal`/`-shm` sidecars;
  for PostgreSQL, a consistent database backup including `_toolframe_*` history);
- `domains/`, especially all migration files;
- `app/`, `APP_CONTEXT.md`, and `DOMAIN_MODEL.md`;
- `workspace/`; and
- `.toolframe/logs/` when diagnostic history is needed.

Applied migration bytes are immutable. Never edit, rename, move, or delete an
applied revision. New domains use owner-local revisions and explicit dependencies.

## Doctor: report first, repair only when asked

Do not delete the database, runtime, migrations, app, or domains as a first
response. Capture:

```sh
./toolframe check --format json
./toolframe --help
```

Save complete stderr and, when present, the relevant files under
`.toolframe/logs/`. Identify the exact path, phase, exception, and trace ID.
Then explain the cause. Source edits require an explicit repair request and are
made by the master development agent.

| Failure | Retry? | Safe response |
| --- | --- | --- |
| Temporary UV/package/network failure | Usually | Restore connectivity and retry; use offline only with a complete cache. |
| Port unavailable | Sometimes | Stop the competing service or choose an approved port. |
| Database busy/transient I/O | Usually | Stop competing writers, verify disk health, then retry. |
| Migration gap, duplicate, bad SQL, or checksum drift | No | Restore historical bytes or append an owner-scoped revision; never erase data/history. |
| Dependency conflict or wrong Python range | No | Correct `app/pyproject.toml` or public tool metadata, then check/restart. |
| Syntax or domain-boundary error | No | Repair the named public source through the master agent. |
| Tool identity/decorator/schema/import failure | No | Repair the named tool and restart; it cannot be skipped. |
| Widget/API contract or import failure | No | Repair the named domain source and restart. |
| Expected user conflict shown in one error card | Usually no until user action | Follow the card's recovery step or copy it to the master agent. |
| Unexpected dashboard programming exception | No | Use stderr/trace ID to repair source; do not turn it into another friendly card. |
| MCP tool error | As declared | Read code, retryability, recovery hint, and trace ID; repeat writes only with idempotency/guards. |
| Logging failure | No | Repair permissions/disk/competing instance; logging is fail-closed. |

After an authorized repair, rerun check, start the server, verify `/healthz`,
complete the dashboard action, and list/call MCP tools as appropriate.

## Logs and sensitivity

Default logs:

```text
.toolframe/logs/audit-YYYY-MM-DD.jsonl
.toolframe/logs/mcp-YYYY-MM-DD.jsonl
```

The audit stream records lifecycle and application mutations. The MCP stream
records accepted traffic and results. Payloads can contain business data and
secrets; share only with trusted maintainers. Large values are truncated with
byte count and SHA-256 evidence, not redacted.

Default retention is 14 UTC date buckets and 256 MiB combined. Unknown files
are preserved. A required logging/retention failure stops the process or
prevents an operation from being reported as successful.

## Hard boundary

Move to a dedicated application when the requirement includes public Internet
access, authentication/authorization, untrusted users or source, tenant
isolation, high availability, multiple processes/databases, durable background
workers, high write contention, strict compliance/backup guarantees, or a
polished real-time/mobile client. Do not bolt authentication onto this local
runtime and call it production isolation.

## FAQ

### Which URL should a peer receive?

The current printed LAN URL ending in `/llms.txt`. The peer derives same-origin
`/mcp` and proves registration by listing tools.

### Why did a new tool not appear?

Tools are startup-loaded. Confirm it is a direct public file under the correct
domain, run check, then fully restart.

### Why did a dashboard edit appear without restart?

Streamlit reruns dashboard code. That does not reload migrations, MCP tools,
dependencies, or a changed LLMS allowlist.

### What happens when one tool or widget is broken?

The complete app is unhealthy. Startup/check fails with the named source; no
domain or tool is silently omitted.

### Does the browser or peer get raw SQL?

No. The browser uses controlled widget actions and peers use named business
tools. The private SQL reference is not published.

### Can peers read `APP_CONTEXT.md` or `DOMAIN_MODEL.md`?

No. Those are local master-agent documents. Only `app/llms.txt` and its
explicit `/llms/<domain>/<name>.md` links are served.

### Is `/healthz` business health?

No. It proves process readiness. Use the dashboard, check output, tool calls,
and business evidence to verify the workflow.
