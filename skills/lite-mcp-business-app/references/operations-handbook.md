# Operations handbook

This is the owner-facing operating guide for a `lite-mcp` folder. It explains
what the executable is doing, how to find the one useful address, how to
diagnose a failed start, and when this small local app has reached its limit.
It is also a briefing for the coding agent maintaining the business app.

## What the app is

`lite-mcp` is a small, LAN-first business workspace contained in one editable
folder. Starting the executable opens one browser dashboard for people and one
MCP address for trusted peer agents on the same port. Business data stays in
`data.db`; the dashboard, agent actions, database changes, working files, and
logs live in clearly named folders beside it.

The app is useful when a trusted team wants a quick place where:

- a scheduled or peer agent can ask for work that is ready;
- an agent can read the user’s instructions and current context;
- an agent can write progress and a result;
- a person can open the dashboard over the LAN to inspect, add, review, or
  advance business items.

The executable does not know the business meaning of a “task”. The coding
agent gives it that meaning by adding a SQLite migration, MCP tools, and
Streamlit widgets. It also does not contain a scheduler or durable worker
queue: a scheduler or agent outside the process must make the MCP call.

The app is deliberately trusted and local. It has no authentication, no
authorization model, and no Python sandbox. Any reachable LAN client can use
the dashboard, call published tools, and reach data those tools expose.

## Start and connect

Run from the packaged folder:

```sh
./lite-mcp
```

`./lite-mcp serve` is the explicit equivalent. On the first online start, UV
may download Python 3.14 and the shared dependencies into `runtime/`. The UV
executable is selected in this order: an explicit `--uv-path` (or
`LITE_MCP_UV`), the `uv` file beside `lite-mcp`, then `uv` on `PATH`. The
packaged folder normally already has the sibling `uv`; an installed system UV
is the fallback. Use the local README or `./lite-mcp --help` for current flags.
`--offline` only works after the required Python and packages are cached.

The terminal prints several useful addresses. The first local address is for
the same computer:

```text
Dashboard  http://127.0.0.1:8000/
Info       http://127.0.0.1:8000/info
MCP        http://127.0.0.1:8000/mcp
Agent guide http://127.0.0.1:8000/llms.txt
LAN        http://192.168.1.20:8000/
```

Use the printed `LAN` address as the single recommended address for another
computer on the same network. The launcher intentionally prints only its
preferred LAN address (normally one usable private IPv4 chosen by the
launcher), rather than
listing every Wi-Fi, virtual, link-local, or benchmark interface. Other
interfaces may exist but are not promises that the route will work. If the
preferred address cannot be reached, check that both computers are on the same
LAN and that the host firewall permits the selected port, then inspect the
actual network configuration.

For a peer coding agent, first give the LAN address with `/llms.txt` appended:

```text
http://192.168.1.20:8000/llms.txt
```

That short document links to the business context, actual capabilities,
workflow, and operations guide. It instructs the peer to use its own client's
MCP registration flow for same-origin `/mcp`, then list the actual tools before
claiming the connection works. Reading the document alone cannot install MCP
into every client or bypass an approval step. If the client cannot register an
MCP server, it should return the name, URL, Streamable HTTP transport, and exact
blocker instead of inventing a command.

The MCP transport is Streamable HTTP only. There is no SSE endpoint and no
stdio endpoint. `/` is the Streamlit dashboard, `/info` is the built-in
diagnostic page, and `/healthz` is the simple health check. The supporting
diagnostic routes `/api/database` and `/api/runtime` remain available to the
built-in info page and compatibility tooling.

## The normal owner loop

1. Start `lite-mcp` in the app folder.
2. Open the printed LAN dashboard address in a browser.
3. Use the dashboard to see the business items, their status, the latest
   progress, and the result or review request.
4. Give the peer agent the printed LAN Agent guide URL. It reads only the
   linked pages relevant to the request, registers same-origin `/mcp`, proves
   the connection by listing tools, then uses the app's narrow business tools.
5. Review the item in the dashboard. The owner or an agent can write the next
   business action through a widget or a controlled tool.

The exact task names and widgets are business-specific. The coding agent must
keep the owner’s instructions visible in the app’s context documentation and
must make state changes explicit, reviewable, and idempotent where a caller
may retry.

## When a workflow cannot reach MCP

Connection refused, timeout, DNS failure, a wrong route, or a stopped server
can prevent a scheduled/peer agent from reaching `/mcp`. At that point the
agent does not know which business item is pending and cannot truthfully write
a Job outcome. Keep the workflow paused at the integration boundary; do not
turn missing access into `failed`, `blocked`, `done`, or another business state.

Return two things in the workflow response:

1. a visible **[app name] MCP unavailable — workflow paused** warning stating
   which reads/writes did not happen and that no business-state change is
   known; and
2. one fenced Markdown issue the owner can copy as-is to the maintainer.

Read and fill the
[MCP-unavailable response template](../assets/MCP_UNAVAILABLE_ISSUE.template.md).
Use observed values or `Unknown`, preserve the attempted URL and exact client
error, and distinguish “this peer cannot reach the route” from evidence that
the server process itself stopped. Retry only after the stated prerequisite is
corrected; a later scheduler run is a new attempt, not proof that an earlier
mutation failed.

If MCP returned a structured tool/business error, use that error's code,
retryability, and recovery hint instead. The unavailable template applies when
the intended MCP business action did not complete because transport, route, or
server availability failed.

## What changes require a restart

Use the executable as a startup contract:

- Migrations are discovered, checked, and applied before the HTTP listener is
  ready. They run in numeric order and an error fails the whole app.
- MCP tool files and their dependencies are loaded once at startup. Adding or
  editing a `tools/*.py` file has no effect until the complete process is
  stopped and started again.
- Tool discovery ignores hidden files, underscore-prefixed files, symlinks,
  and nested directories. Published files must satisfy the Python 3.14 and
  dependency metadata contract described in the local README.
- Dashboard source is also validated at startup, but Streamlit reloads the
  dashboard source during a browser refresh or rerun. This is dashboard
  rerun, not MCP tool hot reload.
- Existing startup-approved `llms.txt` and `llms/*.md` files are read again for
  each request, so editing their content does not require a restart. Adding or
  renaming a linked detail file changes the startup allowlist and requires a
  complete restart. Unsafe, missing, invalid UTF-8, oversized, or unlinked
  agent documents make startup/check fail instead of publishing partial
  guidance.
- There is no reload command, reload API, or reload button in this MVP.

Before a restart, run the non-destructive preflight:

```sh
./lite-mcp check
./lite-mcp check --format json
```

Use the JSON result when a coding agent needs a machine-readable gate. Check
uses an isolated database snapshot, validates migration history and the
agent-document set, resolves the real shared UV environment, loads every tool,
and validates the dashboard and widgets. It does not mutate the configured
`data.db`, bind a port, create server logs, or scaffold missing source. It can
still provision managed
runtime/dependency files, and importing trusted Python may have import-time
side effects. Stop the running app and other database writers before checking
a live database so it cannot change during the snapshot.

For a new source file, these commands are safe and refuse to overwrite an
existing file:

```sh
./lite-mcp scaffold migration add_status
./lite-mcp scaffold tool fetch_status
./lite-mcp scaffold widget status_card
```

Schema history belongs in a new numbered migration authored by the primary
development agent. The `execute_sql` fallback rejects schema changes and is
only for bounded app-data reads/DML. Never edit, rename, or delete an
already-applied migration. Back up the app folder, especially `data.db`, its
SQLite `-wal`/`-shm` sidecars when present, `migrations/`, `tools/`,
`dashboard/`, `workspace/`, and `logs/`, before a consequential change. Stop
the process before copying the database or sidecars.

## Doctor: report first, then repair

Do not delete `data.db`, `runtime/`, migrations, logs, or user source as a
first response. First collect facts and preserve the failure:

```sh
./lite-mcp check --format json
./lite-mcp --help
```

Save the check report and the complete terminal error output. If the app has
started, also save that date's two log files:

- `logs/audit-YYYY-MM-DD.jsonl` records application changes and lifecycle
  events;
- `logs/mcp-YYYY-MM-DD.jsonl` records peer-agent MCP traffic.

Each line is a machine-readable JSON record intended for the coding agent doing
the diagnosis. The files can contain database statements, submitted values,
results, and secrets; share them only with a trusted maintainer and remove or
restrict copied files afterward. There is no browser log viewer.

Then classify the failure:

| Symptom or cause | Retryable? | First safe action |
| --- | --- | --- |
| UV download timeout, temporary package mirror failure, or transient network I/O | Usually yes | Keep the folder, restore connectivity, then retry. Use `--offline` only when the cache is known complete. |
| UV missing, invalid, or too old | No until fixed | Check the explicit path, the sibling `uv`, or the PATH installation; use `--help`/local README for current requirements. |
| Port already in use | Sometimes | Stop the other service or choose an approved port; the launcher searches its configured range, but a fully occupied range is a real failure. |
| Database busy during a snapshot or transient disk I/O | Usually yes | Stop other writers, wait for the owner operation to finish, confirm free disk space, and retry. |
| Migration SQL/name error, altered applied migration, checksum drift, incompatible history, or constraint failure | No | Fix the migration source or add the next migration. Do not rewrite applied history or erase the database. |
| Dashboard dependency metadata or dependency conflict | No | Fix the dashboard manifest/dependency declaration, run `check`, and restart. |
| Tool syntax/import/decorator/schema/identity failure | No | Read the reported tool path, load phase, cause, traceback, and UV/Python diagnostics; repair the file, then restart. |
| Dashboard loader/widget contract or startup import failure | No | Repair the named dashboard file/widget and restart. A dashboard startup failure stops the whole app. |
| Uncaught dashboard exception during a later rerun | No until fixed | Use the displayed trace ID and server traceback to repair the widget, then restart; the launcher does not auto-restart. |
| JSONL setup, serialization, append, flush, rollover, retention, or close failure | No until fixed | Check the logs path, permissions, disk space, and competing instance; logging is fail-closed, so the process exits. |
| MCP request rejected or one tool call returns an error | Depends on the response | Read `code`, `message`, `retryable`, `details`, and `traceId`. Retry only when `retryable` is true and the business operation is safe to repeat. |
| Reference SQL tool reports busy/locked or transient disk I/O | Usually yes | Wait for the writer and retry the one statement. |
| Reference SQL tool reports syntax, constraint, empty, oversized, missing-environment, or multi-statement input | No | Correct the request; use a migration for durable schema changes. |

The launcher includes the failing file and startup phase where available. A
failed agent action includes a reference ID (`traceId`), reason, whether the
same request may be tried again, and a recovery hint; full detail is in the
terminal output and log files. A tool that fails discovery, import, or startup
validation does not silently disappear: the app refuses to start. A later tool
invocation error returns its structured error and does not by itself unload the
tool or stop an otherwise healthy server.

After a repair, run `check` again, then start the server and verify all three
surfaces: the dashboard `/`, `/healthz`, and the MCP `/mcp` URL. A successful
`/healthz` means the server lifespans are ready; it does not prove that every
business callback, upstream service, or user input will succeed.

## What the log files contain and retain

The app writes two dated log files under `logs/`. They are plain text with one
machine-readable JSON record per line, so a coding agent can inspect them even
when the app will not start:

```text
logs/audit-YYYY-MM-DD.jsonl
logs/mcp-YYYY-MM-DD.jsonl
```

The MCP log records accepted peer-agent traffic in both directions, tool calls,
results, the client name/version when declared, and the source IP/port. The
audit log records Dashboard/database and managed-file changes, rejected
requests, app start/stop/fatal events, and log cleanup/recovery events.
Reference IDs connect a Dashboard or MCP action to its detailed failure.

Submitted or returned content larger than 16 KiB is shortened by keeping its
beginning and end and removing the middle. The log retains the original byte
count and a SHA-256 fingerprint so a maintainer can identify the original
content without storing all of it. Error traces have their own 64 KiB limit.
Nothing else is hidden automatically. Treat logs as sensitive business data.

By default, date buckets are retained for 14 UTC days and the two streams have
a combined 256 MiB cap. Old audit/MCP files are removed as date pairs; unknown
files are preserved. A partial final JSONL line is trimmed to the last complete
newline at startup and recorded as a recovery event. If writing or retaining a
required record fails, the current operation is not declared successful and
the Python runtime and launcher fail closed.

## Hard boundary: use a dedicated development app

Keep this app for one trusted LAN, one business workspace, quick iteration,
small or moderate SQLite data, and short-lived agent interactions. Escalate
the product to a dedicated development app when any of the following is a
requirement rather than a future possibility:

- public Internet access, authentication, authorization, role separation, or
  audit guarantees for untrusted users;
- untrusted tool/dashboard code, tenant isolation, customer data separation,
  or secrets that must not be visible to any LAN participant;
- multiple tenants, multiple installations with shared state, high
  availability, failover, zero-downtime deployment, or multi-process scale;
- durable background workers, a real queue/scheduler, long-running jobs,
  cancellation guarantees, automatic retries, or exactly-once external side
  effects;
- high write throughput, large datasets, concurrent writers beyond what a
  small SQLite app can comfortably handle, or a cloud/remote database;
- regulated data, formal compliance controls, tamper-evident audit, backup and
  restore guarantees, or security review requirements;
- polished public UI, mobile clients, real-time collaboration/push, complex
  frontend state, or a custom API contract beyond the provided Streamlit and
  Streamable HTTP surfaces.

Do not try to add authentication as a local patch to this handbook’s MVP.
When a boundary is reached, give the dedicated development agent the business
objective, current schema/migration history, domain model, tool contracts,
dashboard behavior, sample trace IDs, and the reason for escalation. Preserve
the working folder as a reference until the replacement is verified.

## FAQ

### Why is there only one LAN address?

The launcher chooses one preferred usable LAN address so an owner has one
clear copy/paste target. Virtual and link-local interfaces are intentionally
not advertised. The address may change when the network changes; read the
current startup output.

### Can I use `127.0.0.1` from another computer?

No. That address means “this same computer” to the caller. A peer agent needs
the current printed LAN address and `/llms.txt` first; a person needs the same
LAN host address without a suffix for the dashboard.

### Which URL should I give a new peer agent?

Give it the current printed LAN address ending in `/llms.txt`. That is the
progressive discovery guide; it tells the agent how to derive same-origin
`/mcp`, which linked context to read, and how to prove registration by listing
tools. Give `/mcp` directly only when the client's MCP registration flow asks
for the endpoint URL.

### Do edits to the agent guide need a restart?

Content edits to existing approved `llms.txt` and `llms/*.md` files appear on
the next request. Adding, removing, or renaming a detail file needs a restart
because the safe file set is fixed at startup. Run `check --format json` before
restart; `check` validates existing guide source but never creates or repairs
it.

### Does the browser accept arbitrary SQL?

No. There is no browser API for arbitrary SQL. Dashboard widgets should use
the shared database helper and parameterized SQL for controlled reads and
writes. `tools/execute_sql.py` is a trusted, bounded data read/write fallback
and adapter reference. It rejects schema, PRAGMA, maintenance, attached
database, and runtime-owned table changes; migration history is the only schema
path.

### Why did my new tool not appear?

Tools load only when the process starts. Stop and start `lite-mcp`; there is no
MCP reload feature. Run `check` before the restart to get the error while the
source is easy to inspect.

### Why did my dashboard change appear without a restart?

Streamlit reruns the dashboard source on browser refresh or a widget rerun.
This applies to dashboard code only. A dependency or import-contract change
still needs a full restart.

### What happens if one tool cannot load?

The app fails to start and reports the tool path, phase, reason, retryability,
trace ID, and diagnostic detail. Fix the named tool and restart; the tool is
not silently skipped.

### What happens if a dashboard widget fails later?

An uncaught widget failure is fatal to the whole app. The runtime logs the
widget, phase, exception, trace ID, and traceback, then exits. Expected user
or database errors can remain local to the widget when the widget catches and
renders them explicitly.

### Does the app run a schedule for me?

No. It provides the place where an external scheduler or agent can call tools
to find ready work and write results. If the schedule must survive process
restarts, guarantee delivery, or coordinate many workers, use a dedicated
queue/worker application.

### Can I send the MCP URL over the Internet?

No. The endpoint is unauthenticated trusted LAN infrastructure. Do not expose
the port publicly or rely on the endpoint for identity, tenant isolation, or
secret protection.

### What does “retryable” mean?

It means the immediate cause may clear without changing the business request,
such as a busy database or temporary upstream/network failure. It is not a
promise that repeating a mutation is safe. Use an idempotency key or a
business-specific guard for retryable writes, then inspect the trace and
result before repeating.

### Why are logs large or sensitive?

MCP and dashboard mutations are recorded for local diagnosis. Large payloads
keep only their head and tail, but there is no secret redaction. Keep `logs/`
private, adjust the documented retention settings only when needed, and do
not paste raw logs into an untrusted chat.

### Is `/healthz` a business health report?

No. It is a simple server readiness check. Use `/info`, `check`, the dashboard,
and the relevant logs to determine whether a particular migration, widget,
tool, or business operation works.

### What should a scheduled workflow return when MCP is unavailable?

It should pause, say that pending work was not queried and no business state
was changed, then include the complete copyable issue from the installed
skill's MCP-unavailable response template. It should not silently succeed,
invent a Job failure, or require the owner to reconstruct the URL and error
from earlier messages.
