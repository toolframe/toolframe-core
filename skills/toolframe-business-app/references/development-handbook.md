# Development handbook

This is the build playbook for the master development agent working in one
editable Toolframe distribution. Begin with the business outcome, then make the
smallest domain-owned change that lets a human or peer agent complete it.

The local `README.md`, `./toolframe --help`, current source, and check output are
the runtime authority. Reconcile this handbook when those facts differ.

## 1. Fit and name the app

Ask in business language:

- What real-world item is created, found, changed, reviewed, or completed?
- Who submits it, acts on it, reviews it, and only observes it?
- What must a peer agent do unattended through MCP?
- What result or history proves success?
- What should the owner see first?

Toolframe fits one trusted LAN, one bounded workflow, one selected SQLite or PostgreSQL database, a
Streamlit human surface, and a small set of trusted MCP tools. Escalate before
implementation for public access, authentication/permissions, untrusted code,
tenants, high availability, durable workers, heavy concurrent writes, multiple
databases, or formal compliance requirements.

Toolframe is the framework, not the app name. If the scaffold still says
`Unnamed`, suggest two or three short names grounded in the business noun or
outcome and recommend one. Apply the chosen or explicitly provisional name to:

- `APP_CONTEXT.md` as the authority;
- `app/dashboard.py` for the browser-facing identity; and
- `app/llms.txt` plus relevant linked peer documents.

## 2. Inspect the complete contract

Run:

```sh
./toolframe check --format json
./toolframe --help
```

Inspect git status, `APP_CONTEXT.md`, `DOMAIN_MODEL.md`, `app/`, and every
relevant `domains/<domain>/` subtree. The current editable contract is:

```text
APP_CONTEXT.md
DOMAIN_MODEL.md
app/{dashboard.py,llms.txt,pyproject.toml}
domains/<business_domain>/{__init__.py,llms,migrations,model,tools,ui}
```

Do not introduce legacy top-level feature folders or a generic `shared/`
package. `serve` may create a complete source root only when that whole root is
absent. An existing partial root is a defect to diagnose, not permission to
auto-repair or overwrite files.

`check` validates schema dependencies and archived history without mutating the configured
database, resolves the real dependency environment, compiles public Python,
enforces domain import boundaries, imports every public tool and widget, and
validates only the LLMS documents explicitly linked by `app/llms.txt`.

## 3. Maintain the private sources of truth

Use the app-context and domain-model assets for the two private root documents.

`APP_CONTEXT.md` exists for the master development agent. Record the app name,
purpose, actors, business loop, boundaries, interfaces, domain ownership,
decisions, operating state, verification, and next safe change.

`DOMAIN_MODEL.md` aligns implementation with the user's language. Record
entities, identities, invariants, states/transitions, concurrency rules,
storage mapping, and business meanings of errors. Read `job-domain-model.md`
only when the app owns queued, claimed, leased, retried, or reviewed agent work.

Neither private document is a peer-agent endpoint. Project only the minimum
supported operating contract into `app/llms.txt` and its linked domain docs.

## 4. Choose a real business domain

A domain owns a cohesive vocabulary, invariants, lifecycle, model, schema,
tools, UI, and peer instructions. Extend an existing domain when the new
behavior follows the same language and rules. Create a new one only when it is
a distinct bounded business capability:

```sh
./toolframe scaffold domain <business_domain>
```

Within a domain, use relative imports or its full own path freely. Across
domains, consume only explicit high-level business functions exported by the
provider's root `__init__.py`:

```python
from domains.inventory_control import reserve_stock
```

Never reach into another domain's `model`, `tools`, `ui`, `llms`, or
`migrations`. Do not export storage adapters, table objects, widget functions,
or low-level CRUD through `__init__.py`. A cross-domain call should express one
business capability and exist only when the caller truly needs it.

The composition root `app/dashboard.py` is unrestricted. It may use the
automatic widget registry, select part of it, or compose UI directly.

## 5. Build one vertical business loop

### A. Add durable schema in the owning domain

Read [plugins-and-data.md](plugins-and-data.md) for the schema manifest, provider
variants, service boundaries and preservation contract. Create an owner-scoped draft with:

```sh
./toolframe scaffold migration <domain> <description>
```

Complete the generated SQL variants and owned `tables` in `schema.json`.
Preserve applied bytes and metadata; add a revision with explicit dependencies.
Original legacy numbering stays intact.

Business tables use the owning domain prefix, such as `inventory_control_stock`
or `job_desk_jobs`. `_toolframe_*` tables belong to the runtime. Keep schema
administration out of MCP and the dashboard. Do not put transaction control,
connection PRAGMAs, attached databases, or triggers in migration source; the
Python coordinator owns migration execution and durable artifacts/history.

Prefer durable business history: stable identities, UTC timestamps, actor
labels, explicit versions, and append-only events/results when reconstruction
matters. Back up the selected database before destructive data work; include sidecars for SQLite.

### B. Put rules in `model/`

Keep business validation, state transitions, persistence operations, and
domain-specific error types in `domains/<domain>/model/`. UI and MCP tools call
this layer rather than importing each other. Make every invariant authoritative
in one place and reinforce critical rules with database constraints.

Model modules are compiled during startup validation but are not eagerly
executed unless imported. Public model source must still be valid and respect
cross-domain boundaries.

### C. Publish narrow Business RPC tools

Create a direct public file:

```sh
./toolframe scaffold tool <domain> <globally_unique_name>
```

Each `domains/<domain>/tools/<name>.py` publishes exactly one synchronous
FastMCP tool whose function and MCP name match the filename. Public names are
globally unique. Declare Python 3.14 and exact needed packages in PEP 723
metadata. Keep private helpers in `_*.py` or the domain model.

A command completes one guarded business intent or transition. A query answers
one bounded decision question. Do not publish generic create/read/update/delete,
field patching, arbitrary filters, SQL, schema changes, repair, or inspection of
private runtime state.

Use:

- `toolframe_db.get_database()` for bounded parameterized query/data DML;
- `toolframe_idempotency.run_once()` around complete replayable mutations;
- `toolframe_artifacts.get_artifact_store()` for create-once large outputs; and
- `toolframe_runtime.ToolframeError` for structured business/tool failures.

Use the configured database service in public tools. The private
`_execute_sql_reference.py` demonstrates adapter mechanics but is not an MCP
capability and must never appear in peer docs. When a peer needs unsupported
behavior, implement a new narrow business tool through the master agent.

### D. Add the human decision surface

Create a direct public widget:

```sh
./toolframe scaffold widget <domain> <name>
```

A public `domains/<domain>/ui/<name>.py` exports synchronous `render(context)`.
Optional `TITLE` and integer `ORDER` control the registry; explicit order values
must be globally unique. `_*.py`, dot files, and nested directories are not
auto-loaded widgets.

Use Streamlit forms, tables, editors, metrics, and buttons to expose the
business loop. Bind input through `context.db` and call the same domain model
rules as tools. Use expected-version or lease guards when stale writes matter.
Never accept arbitrary browser SQL.

For expected recoverable user conditions, create a
`DashboardInteractionError` and pass it to the single core
`render_error_card()`. The result must be one copyable card with a clear message,
recovery action, trace ID, and master-agent escalation. Do not build shared or
per-domain error card components. Programming errors are fatal and must not be
converted into a friendly local card.

### E. Publish only peer operating guidance

`app/llms.txt` is authored source served at `/llms.txt`; Toolframe does not
generate a catalog into it. Keep it concise: name the app, state the trusted-LAN
boundary, identify same-origin `/mcp` and Streamable HTTP, require a successful
tool-list proof, and link only relevant domain docs.

Valid detail links have the form:

```text
/llms/<domain>/<name>.md
```

The target is `domains/<domain>/llms/<name>.md`. Only explicitly linked regular
Markdown files are served. Unlinked files, `APP_CONTEXT.md`, and
`DOMAIN_MODEL.md` remain private.

Linked documents describe actual MCP business capabilities, domain input/output
meaning, and the bounded peer workflow. Do not publish repair steps, source
paths unnecessary for use, internal error catalogs, schema administration,
generic SQL, or tools that do not exist. Tell peers to preserve exact failures
and ask the master development agent for fixes or new capabilities.

## 6. Make failures actionable

Public migration, dependency, syntax, domain API, import-boundary, tool, and
widget failures abort startup. Never rename a broken public file to hide it,
skip its domain, weaken validation, or report a partial catalog as healthy.

Expected invocation failures return stable codes, human-readable messages,
retryability, trace IDs, and recovery hints. Technical retryability is not
business state and does not prove a mutation is safe to repeat.

Doctor flow:

1. Capture `./toolframe check --format json` and complete stderr.
2. Preserve database/migration bytes and relevant JSONL logs.
3. Identify the authoritative source and smallest correction.
4. Repair only when authorized.
5. Rerun check, restart, and prove affected human and peer paths.

## 7. Verify and hand off

Run focused tests plus the repository's formatter/linter. Then require:

1. `./toolframe check --format json` succeeds.
2. Real startup reaches readiness and applies pending migrations.
3. Representative local invocation or MCP calls prove reads, writes, replay,
   stale input, and error behavior when relevant.
4. The dashboard completes the human path.
5. A peer starts from the tested LAN `/llms.txt`, registers same-origin `/mcp`,
   and lists the actual tools before using them.
6. One end-to-end business outcome and its durable evidence are visible.

Reconcile private documents, migrations, model, tools, UI, linked peer docs,
and tests in the same change. In the final report, lead with the business
outcome, then state migration/data effects, tool names, dashboard actions,
tested URLs, evidence, restart requirements, limits, and the next human action.
