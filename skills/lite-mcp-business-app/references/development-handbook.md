# Development handbook

This handbook is the build playbook for a coding agent helping a nontechnical
owner turn one `lite-mcp` folder into a small local business application. Start
with the business outcome, then make the smallest database, tool, and dashboard
change that lets a person or an agent complete that outcome.

The editable app folder's local `README.md` is the current runtime contract.
Use it, `./lite-mcp --help`, and the local source files as evidence when a
command or behavior may have changed. This wording is deliberate: the skill
may be installed separately from the app folder. Do not copy package-version
lists into an app handoff document.

## 1. Fit the request before choosing a design

Ask the owner, in plain language:

- What real-world item should be created, found, changed, reviewed, or
  completed?
- Who creates it, who works on it, who reviews it, and who only needs to see
  it?
- What must a scheduled or peer agent be able to do without a human present?
- What information is the agent allowed to receive, and what result must it
  write back for a human to find?
- What is the one screen a person should open first?

Choose this app when the answer is one trusted LAN, one small business
workflow, one SQLite database, a human-friendly Streamlit screen, and a few
trusted Python tools. It is a quick local adapter for a business requirement,
not a general platform.

Escalate to a dedicated development app before implementation when the request
needs public or internet access, authentication or permissions, untrusted
code/users, multiple tenants, high availability, a durable worker/queue, large
or high-contention data, multiple databases, strict compliance guarantees,
long-running work outside one tool call, real-time push/mobile UI, or a
carefully managed deployment lifecycle. The binary deliberately has no
authentication, has trusted Python dashboard/tool code, and uses one local
SQLite process. Point the owner to the skill's limitations/FAQ rather than
trying to hide those boundaries in schema or UI code.

Do not make the owner decide Python package layout, SQL connection mechanics,
or frontend framework details. Translate their answers into a visible business
loop and give safe defaults. Ask again only when two plausible choices would
change the business result (for example, whether a review is required before
completion).

### Name the app, not the framework

A fresh scaffold deliberately displays `Unnamed`. Lite MCP is the framework
and executable name; it is not the identity of the owner's business app. Once
the business loop is understood, proactively suggest two or three short names
grounded in the main business noun or outcome, mark one recommendation, and
explain it in one sentence. Avoid generic labels such as “Dashboard”, “Tool”,
or “Lite MCP App” that will not distinguish multiple servers on the same LAN.

Let the owner choose when they respond. If implementation must continue before
they answer, use the recommendation as an explicitly provisional, reversible
assumption and record that status in `APP_CONTEXT.md`; do not stop otherwise
safe development only for naming. Before handing off a developed app, make its
chosen or provisional name consistent in all owner- and peer-facing identity
surfaces:

- `APP_CONTEXT.md` App identity — authoritative name and rationale;
- `dashboard/app.py` `APP_NAME` — browser title and visible heading;
- `llms.txt` H1 and summary — LAN agent identity; and
- `llms/context.md` App identity — app name, framework attribution, and rationale.

Completion requires all four surfaces to use the same app name and none to
remain `Unnamed`. Framework diagnostics, package names, CLI commands, and
technical attribution continue to use `lite-mcp` or Lite MCP.

## 2. Inspect the installed contract before editing

Work in the directory that contains the executable. Treat that editable folder
as the application, not merely as a server installation. Before writing code,
inspect:

```sh
./lite-mcp check --format json
```

Read the local `README.md`, `migrations/README.md`, existing migrations,
`dashboard/app.py`, public widgets, and public tools. `check` validates the
production migration path against an isolated SQLite snapshot, resolves the
shared UV project, imports every startup tool, materializes its MCP schema,
and validates the dashboard and widgets. It can provision the managed runtime;
it does not apply changes to the configured `data.db`, bind a port, create logs,
or scaffold missing source directories. Treat the resulting JSON document and
the packaged README as the portable evidence; a release folder does not include
the Rust/Python launcher source tree.

Confirm the current public shape before relying on it:

- `./lite-mcp` or `./lite-mcp serve` starts the one-process LAN service.
- `/` is the Streamlit dashboard, `/info` is the built-in runtime/datastore
  page, `/llms.txt` is the progressive LAN-agent entry point, and `/mcp` is
  FastMCP Streamable HTTP. There is no SSE, client stdio, hot reload, reload
  API, or reload UI.
- Migrations and MCP tools load at process start. Dashboard source is picked
  up on browser refresh, Streamlit rerun, or interaction rerun; tool source,
  migration source, and dependency changes require a full restart.
- `./lite-mcp scaffold migration <description>`, `tool <name>`, and
  `widget <name>` create one new file and refuse to overwrite an existing one.
- `./lite-mcp invoke <tool> --json '<object>'` exercises one startup-loaded
  tool without opening HTTP. It is a coding-agent check, not an external
  scheduler.

The default bind is LAN-facing and unauthenticated. Treat dashboard data,
tool capabilities, inherited environment, and the writable workspace as
visible to every reachable LAN client. The exact current route, path, startup,
and logging contract remains in the app folder's local `README.md`; use that
document instead of repeating volatile details here.

## 3. Leave an app-local handoff for the next agent

Create `APP_CONTEXT.md` in the application folder before the first substantial
change. Begin with the [APP_CONTEXT template](../assets/APP_CONTEXT.template.md)
and replace its prompts with facts. Its App identity is authoritative for the
owner-facing name. It must also explain the purpose in business language, the
actors, the business loop, vocabulary, interfaces, boundaries, operating
instructions, source map, decisions/open issues, and handoff notes.

Create `DOMAIN_MODEL.md` for every business app so its terms, entities,
invariants, and state rules remain explicit. Read
[`job-domain-model.md`](job-domain-model.md) only when work is polled, claimed,
or leased by a scheduled or peer agent and its execution/review belongs to that
queue lifecycle. It defines the recommended finite-state machine and
Job/Context/Attempt/Result/Review vocabulary. Progress, results, or approval in
an ordinary CRUD app do not trigger the Job model by themselves. Do not force
that model onto an unrelated business app such as a catalogue, register, or
approval form; use the smallest domain language that fits in its own
`DOMAIN_MODEL.md` and record the choice in `APP_CONTEXT.md`.

Keep each fact authoritative in one place:

- runtime behavior: root `README.md` and source;
- schema history: `migrations/*.sql` and `migrations/README.md`;
- business vocabulary and state rules: app-local `DOMAIN_MODEL.md`;
- app identity, why the app exists, and how another agent takes over:
  `APP_CONTEXT.md`;
- implementation details: the tool/widget source and their docstrings.
- what a remote LAN agent may discover: the short `llms.txt` index and its
  linked `llms/*.md` projection of those authoritative facts.

At handoff, update both app-local documents with the current schema version,
published tool names, dashboard entry points, unresolved business questions,
and the last verification commands. Never leave an agent to infer purpose from
table names or a screenshot.

## 4. Build in a business-first order

### A. Turn the outcome into a small loop

Write one sentence in `APP_CONTEXT.md`, for example: “A scheduled agent finds
the next approved work item, receives the owner's instruction and context,
records progress, submits a result, and a human reviews it on the dashboard.”
Name the inputs, the decision points, and the evidence a person expects to see
after completion. This sentence is the scope boundary for the first release.

For scheduled work or claims, stop and use
[`job-domain-model.md`](job-domain-model.md) for the state machine, invariants,
lease/claim behavior, idempotency keys, and result/review semantics. The
development sequence below assumes those rules but does not redefine them.

### B. Design the schema as durable history

The `migrations/` directory is the source of truth for `data.db`. Start with
the supplied `0000_bootstrap.sql` and add the next contiguous migration with
the scaffold command:

```sh
./lite-mcp scaffold migration add_business_fields
```

Put schema/data statements in the generated file, then update the tools and
widgets that use them in the same change. Migrations run in numeric order at
startup inside the launcher-owned transaction. Never edit, rename, or delete
an applied migration; restore its exact bytes and add a new migration. Do not
put transaction statements, connection-level PRAGMAs, `ATTACH`, `DETACH`, or
`CREATE TRIGGER` in migration SQL. Back up `data.db` before a destructive
change. Read the app folder's local `migrations/README.md` for the full
authoring restrictions.

Prefer tables that make the business history visible: an enduring business
record, explicit timestamps, an actor or source, and append-only progress or
review records where history matters. Prefix user-owned business tables with
`app_`; `_lite_mcp_*` tables belong to the runtime and remain outside business
migrations and repair SQL. Use foreign keys and checks for rules that must hold
even when two LAN clients write close together. Do not store a large report or
binary payload in every MCP response; use a managed artifact reference when the
result is a file.

### C. Publish one useful tool per file

Create a direct `tools/<lower_snake_case>.py` file with
`./lite-mcp scaffold tool <name>`, then give it one public FastMCP `@tool`
function whose name matches the filename. Keep private helpers in the same
file or in an underscore-prefixed module. Do not publish a collection of
unrelated functions from one file. Put PEP 723 metadata at the top, declare
the Python constraint and only the dependencies the tool needs, and write a
docstring that tells an agent the business purpose, required inputs, output,
and retry meaning.

Good tool boundaries are business actions, not database plumbing. For a task
desk this commonly means “claim the next eligible item”, “get the item's
instruction and context”, “record progress”, “submit a result”, and “review
or request changes”. The exact names and fields belong to the app's domain
model; the principle is that an agent can understand one operation from its
schema and description.

Use the supported helpers before importing lower-level infrastructure:

- `from lite_mcp_idempotency import run_once` for a tool mutation that may be
  repeated after a transport retry;
- `from lite_mcp_artifacts import get_artifact_store` for create-once,
  integrity-checked files under `workspace/artifacts/`;
- `from lite_mcp_runtime import LiteMcpError` to return a structured
  `{code, message, retryable, details, traceId}` failure.

The runtime imports every public tool and materializes its schema before HTTP
readiness. A broken tool therefore fails the whole application at startup.
An invocation failure is different: it returns an MCP error and does not stop
the process. The packaged README is the portable loader and error reference.

### D. Make the dashboard a human decision surface

Create a widget with `./lite-mcp scaffold widget <name>` or add one direct
public Python file under `dashboard/widgets/`. It must export synchronous
`render(context)`. Optional `TITLE` and integer `ORDER` control its label and
ordering. Keep reusable code under `dashboard/components/`; it is not
auto-rendered. Confirm the complete widget contract in the packaged README and
the shipped `dashboard/widgets/example_card.py` reference.

Use ordinary Streamlit controls to show the business loop: the current queue,
what an agent is working on, the latest progress/result, and the next human
decision. Use `st.form`, `st.data_editor`, buttons, and confirmation text for
writes. A widget reads through `context.db.query(sql, params)` and writes
through `context.db.execute(sql, params)` or one
`context.db.transaction()`; bind every value instead of interpolating user
input. Catch expected `DashboardDatabaseError` values and show the code,
trace ID, retryability, and a useful correction in the widget.

For a multi-step mutation that can be sent again, use
`context.db.run_once(namespace, key, input_value, mutation)`. The callback owns
the complete business write transaction. Do not nest it in a dashboard
transaction or use it as a generic automatic retry. A stale row should be
rejected with an explicit conflict and a refresh instruction, not silently
overwrite another LAN session. The packaged README documents the supported
helper surface.

Use built-in Streamlit metric/table/chart components first. Add a custom
component only when the business question cannot be made clear with those
components. Dashboard code is trusted server-side Python, not a browser
sandbox; an app-specific helper does not prevent direct `sqlite3`, filesystem,
environment, or network access.

### E. Publish progressive LAN-agent discovery

Treat `llms.txt` as the copy/paste entry point for a peer agent that cannot
read this folder. Keep the root file short: identify the app by its business
name, attribute Lite MCP only as the framework, state the trusted LAN boundary,
point to same-origin `/mcp`, then link the relevant detail pages.
Put business context, actual capabilities, the operating workflow, and
diagnosis behind root-relative links under `llms/`. Do not copy the whole
README into the root file or link to local filesystem paths a peer cannot open.

Reconcile these default editable files as one set:

- `llms.txt` — index and progressive reading order;
- `llms/agent-guide.md` — client-neutral MCP connection and proof;
- `llms/capabilities.md` — only the tools and Dashboard actions that actually
  exist;
- `llms/context.md` — the app-specific purpose, actors, vocabulary, and source
  of truth projected from `APP_CONTEXT.md` and `DOMAIN_MODEL.md`;
- `llms/workflow.md` — the bounded user-request-to-result sequence;
- `llms/operations.md` — restart, retry, outage, security, and diagnosis rules.

Replace every `CUSTOMIZE` placeholder that the business design resolves. Do
not present the shipped `execute_sql` reference or `example_card` demo as the
production workflow. For a non-queue app, explicitly omit the Job model; use
claim, lease, retry, and review language only when the domain really owns those
concepts. Whenever a tool, schema, widget, business state, or operating rule
changes, update the relevant linked document in the same change.

Write client-neutral connection intent: a peer should use its current client's
MCP registration flow for the same-origin `/mcp` URL with Streamable HTTP,
then list the actual tools. Do not invent a client-specific install command,
claim that reading `/llms.txt` installed the server, or advertise SSE, stdio,
authentication, a scheduler, or capabilities the runtime does not provide.
If the client's registration mechanism is unavailable, report the MCP name,
URL, transport, and exact blocker for the owner instead.

### F. Keep the scheduler outside the app

`lite-mcp` stores business state and exposes the MCP interface; it is not a
cron service or a durable queue. A separate scheduler or agent trigger calls
the published tools over the printed LAN `/mcp` URL. The tool should perform
an atomic eligibility check and claim/lease when scheduled agents can race.
The caller records progress and result through explicit operations, and the
dashboard reads the same rows so the human sees one shared truth.

Do not smuggle scheduling policy into a widget, rely on an in-process timer,
or make “pending” a vague catch-all. Record who owns the next action, when a
lease expires, what attempt produced a result, and which human review is
required when the domain needs those facts. Use the canonical job model for
that design; for a non-scheduled app, omit these tables and tools.

## 5. Make failures useful to an agent

Every public mutation should distinguish a business rejection from a temporary
technical problem. Return a stable error code, a human-readable message, the
`retryable` boolean, a safe recovery hint, and a trace ID. Keep SQL, stack
traces, and sensitive payloads in server-side logs; the browser/MCP response
should contain enough detail for a safe next action, not secrets.

Retry only when the error says it is retryable. A busy/locked SQLite operation
or transient I/O problem may be retried after a short delay. Invalid arguments,
schema mismatch, constraints, conflicts, unsupported SQL, an idempotency-key
input mismatch, an oversized result, and an unmet business precondition are
not fixed by retrying. An ambiguous artifact publication must be verified by
its reference before deciding what to do next. Never blindly retry a mutation
without an idempotency key.

If a tool cannot load at startup, fix the reported path/phase/cause and rerun
`check`; do not ask a scheduler to retry a server that never became ready. If
an expected widget error is caught, render a useful error state so the rest of
the dashboard remains understandable. An uncaught dashboard error is a fatal
application error and requires a full restart after correction.

## 6. Verify the complete operating loop

Run these stages in order and record evidence in `APP_CONTEXT.md`:

1. **Static contract:** `./lite-mcp check --format json` passes; note the
   migration range, tool names, widget names, and dependency result.
2. **Database:** inspect the new migration on an isolated/test database;
   verify constraints, indexes, state guards, and any rollback-safe behavior.
3. **Startup migration:** start the app once so the configured `data.db`
   receives the pending migration; preflight and `invoke` do not apply it.
   Preserve the startup diagnostics and stop the process before a local
   mutation test when that makes the evidence easier to isolate.
4. **Tool contract:** call each new public tool with
   `./lite-mcp invoke <name> --json '<object>'` against migrated state, or call
   the equivalent MCP tool on the running server; verify the success shape,
   structured errors, and retryability.
5. **Dashboard path:** start the app, open `/`, and exercise every changed human
   action in the agreed business loop. Confirm representative reads and any
   writes are visible after a rerun. Test a stale/conflicting write only when
   the app supports concurrent writers.
6. **Peer agent path:** when the agreed app exposes MCP tools, give a trusted
   LAN agent only the current printed `/llms.txt` URL. Confirm every selected
   link resolves, the guide derives the same-origin `/mcp` URL, the client
   registers Streamable HTTP through its supported flow, and an actual
   tool-list response precedes relevant tool calls. Confirm shared state then
   appears in the dashboard. Use the current LAN URL; do not hard-code an
   interface discovered from a previous run.
7. **Restart boundary:** stop and start `lite-mcp` after tool, migration, or
   dependency edits; verify the app fails clearly when an intentionally broken
   source file is present, then restore it and rerun the checks.
8. **Handoff:** update `APP_CONTEXT.md`, `DOMAIN_MODEL.md`, and the progressive
   `llms.txt` document set; list the commands run and their outcomes, name known
   limitations, and state the next business decision. Confirm the Dashboard and
   LAN-facing documents show the same non-`Unnamed` app identity. Keep this
   evidence short and reproducible.

Completion means every capability in the agreed scope works: a nontechnical
owner can understand the relevant dashboard path; a peer agent can use any MCP
surface the app promises; changed writes are durable and safe to repeat where
retries can duplicate them; and another coding agent can continue from the
app-local documents without rediscovering the product.
