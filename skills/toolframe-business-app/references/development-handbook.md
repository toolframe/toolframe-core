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
app/{dashboard.py,llms.txt,pyproject.toml,plugins.py}
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

A domain owns a cohesive vocabulary, invariants, model, schema, business service
plugin, tools, UI, and peer instructions. Extend an existing domain when the new
behavior follows the same language and rules. Create a new one only when it is
a distinct bounded business capability:

```sh
./toolframe scaffold domain <business_domain>
```

Within a domain, use relative imports or its full own path freely. Across
domains, import only the provider's explicit business contract through its root
`__init__.py`. Retained services belong in the consumer plugin's `requires`,
with `ctx.require(KEY)` during setup. For a complete call-time interaction:

```python
from domains.inventory_control import INVENTORY
from toolframe_plugins import use_plugin

with use_plugin(INVENTORY) as inventory:
    inventory.reserve_stock(order_id, items)
```

Never reach into another domain's `model`, `tools`, `ui`, `llms`, or
`migrations`. Do not export storage adapters, table objects, widget functions,
or low-level CRUD through `__init__.py`. If the public API is a convenience
business function, that function must acquire its service lease internally.
Record the consumer's runtime dependency and bind its domain gate as well;
an import alone does not keep a provider active or protect cleanup.

The composition root `app/dashboard.py` is unrestricted. It may use the
automatic widget registry, select part of it, or compose UI directly.

## 5. Build one vertical business loop

### Model durable facts before implementation

Business RPC and dashboard design constrain what callers see; the underlying
database preserves the detail needed to ask other questions about the same
business. Use a normalized relational model by default: each fact has an
authoritative home, and related records connect through stable keys. Define who
retains the inputs before accepting a summary-only boundary: name which future
questions the retained data can answer and which need unavailable detail. Keep
the model within the agreed business scope; no speculative enterprise model is
required.

The owning domain and business service retain authority over these records.
Across domains, consume public business services; migration access and provider
SQL follow [plugins-and-data.md](plugins-and-data.md). Relationships and reporting
needs do not authorize deep imports or bypassing a service through its tables.

Before implementation or writing SQL, complete the data model using these rules:

1. **State the grain.** Describe what one row represents in every table, such
   as one order, one order line, or one payment. Preserve the finest meaningful
   detail available from the agreed input, including quantities, units, relevant
   timestamps, stable source IDs, and facts needed for later joins. If the source
   supplies only monthly totals, record that limitation; never invent line data.
   Distinguish an enduring entity from its participation in a list, program, or
   transaction when those have independent state. An import row number locates
   source evidence; it is not by itself a durable real-world identity. Resolve
   ambiguous identity with evidence, preserving uncertain matches for review.
2. **Define relationships.** Record one-to-one, one-to-many, or many-to-many
   multiplicity, optionality, keys, uniqueness, and update/delete behavior.
   Use link tables for many-to-many relationships and relational columns for
   fields used in joins, filters, and constraints. JSON may hold flexible source
   payloads or metadata; it does not replace known business relationships.
   Enforce local rules with keys/checks and cross-row rules with constraints
   supported by the selected provider or guarded transactions in the owning
   business service. Separate foreign keys alone do not prove that two
   referenced rows belong to the same business record.
   Evidence supports only the relationships its source establishes. If the
   relevant parent or participation is unspecified, keep that association
   unresolved; nearby activity is not evidence for assigning it. Record a later
   owner decision separately from the original observation.
3. **Preserve occurrence meaning.** Separate current state, observed events,
   and inferred milestones. A later stage can imply funnel membership without
   establishing when earlier stages happened. Store known occurrence time and
   its precision separately from recording/import time; preserve unknown dates
   and label inference. Give distinct interactions their own identities, even
   when they share a type and day. A retry key identifies one occurrence; daily
   unique-person counts belong in a query rather than an event uniqueness rule.
4. **Trace recoverable inputs.** Record where imported facts came from, their source
   identity, import time, and transformation/version information when available.
   Retain original imports when audit or reprocessing needs them, especially
   when normalization drops relevant source detail; use managed artifact
   references for files. Document retained/discarded data and retention rules.
   “Raw” does not mandate storing every field or secret forever.
   When input retention belongs to another system, record its owner, immutable
   snapshot/version reference, retrieval path, and retention/backup contract.
   Verify that the needed inputs can be retrieved or travel with the handoff;
   a mutable path on the original machine alone does not prove reproducibility.
   Include upstream stores and retained artifacts in the ownership/backup map;
   the app still uses one selected database service.
5. **Derive business views.** Build totals, display labels, combined rows, and
   dashboard/API shapes with queries or SQL views over retained facts. Keep the
   public API focused on business intent. Add a stored derived table only for
   an evidenced need; document its grain, sources, calculation, freshness rule,
   refresh owner, and complete rebuild path. It is never the only copy of the
   facts used to create it.
   For overlapping memberships, state whether a measure counts distinct
   entities or allocates values across groups so joins do not inflate totals.
6. **Preserve historical business facts.** An issued order's agreed price, an
   approved calculation with its inputs/rule version, or an immutable instruction
   snapshot has meaning at a specific time. Retain it even when today's master
   data changes. Distinguish these records from disposable display caches;
   derivation at creation time does not make an issued fact disposable.
   A concurrency counter or receipt is not a copy of past values. When revisions
   matter, retain the values or immutable payload for each revision and link it
   to the input, classification, and rule versions used. For sent or issued
   content, retain the actual content and recipient/context at that time; a
   reference to the current template cannot reconstruct a past message.
7. **Set evidence lifecycles.** Separate disposable workflow records from
   evidence still required by surviving business records. Record what deletion
   removes, restricts, archives, or retains independently, according to the
   owner's retention policy. Apply this explicitly to imported source evidence,
   historical values, and artifacts as well as ordinary child rows.

For example, a monthly sales card can read a grouped query over `sales_orders`
and `sales_order_lines`, with customer/product relationships and prices at sale.
Those same records can later answer quantities sold by product. Saving only
`(month, sales_total, order_count)` would lose that ability. Start with queries
and appropriate indexes; this policy requires neither a data warehouse nor a
separate event-sourcing system.

**Schema design gate:** every authoritative table has a declared grain and
identity; every relationship has explicit integrity and lifecycle rules; every
derived view traces to recoverable inputs; and unavailable detail, inference,
or intentional data loss has a recorded consequence. History-bearing records
state their time meaning and retained revision values. Proceed to migration
only after the applicable decisions are present in `DOMAIN_MODEL.md`.

### A. Define and register a meaningful business service

Read [plugins-and-data.md](plugins-and-data.md) before every new capability or
business-service change, including work with no migration. Use the copyable
[Stockroom application](../assets/business-plugin/README.md) as an executable
reference, then adapt its business language and invariants.

Define one public Protocol and `ServiceKey` for the capability, normally in
`model/contract.py`, and export the contract through the domain root. Implement
it in the owning model. Put its concrete `Plugin` definition in `model/plugin.py`
with `provides`, `requires`, validated configuration and any actual resource
setup/cleanup. A service that only uses the host database need not invent a
background task or an empty cleanup function.

`app/plugins.py` assembles definitions and binds each domain to its business
service through `DOMAIN_PLUGINS`. The host already supplies `domain.<name>`:
that gate controls MCP/widget availability and must depend on the real service.
Finish this design step when ServiceKey, implementation ID, schema owner and
host gate have distinct names and a connected dependency graph. Each function
or entity stays inside that capability; small edits extend its existing plugin.

### B. Put rules in `model/`

Keep business validation, state transitions, persistence operations, and
domain-specific error types in `domains/<domain>/model/`. The business service exposes these rules; MCP and UI
lease that service rather than calling each other or opening storage directly. Make every invariant authoritative
in one place and reinforce critical rules with database constraints.

Model modules are compiled during startup validation but are not eagerly
executed unless imported. Public model source must still be valid and respect
cross-domain boundaries.

### C. Add durable schema in the owning domain

Read [plugins-and-data.md](plugins-and-data.md) for the schema manifest, provider
variants, service boundaries and preservation contract. Create an owner-scoped draft with:

```sh
./toolframe scaffold migration <domain> <description>
```

Complete the generated SQL variants and owned `tables` in `schema.json`.
Preserve applied bytes and metadata; add a revision with explicit dependencies.
Original legacy numbering stays intact.

Local domain manifests are discovered automatically; the business plugin declares
`requires=(DATABASE,)` so setup waits for schema readiness. A separately packaged
plugin may instead supply `SCHEMAS = {PLUGIN.id: schema}` in the catalog. Use one
declaration for each owner, not both discovery and a duplicate SCHEMAS entry.
Add genuine extra package requirements to `app/pyproject.toml` before restarting;
local business plugins do not need an independent PyPI project. Finish with a
valid catalog/binding and complete schema plan before invoking business code.

Business tables use the owning domain prefix, such as `inventory_control_stock`
or `job_desk_jobs`. `_toolframe_*` tables belong to the runtime. Keep schema
administration out of MCP and the dashboard. Do not put transaction control,
connection PRAGMAs, attached databases, or triggers in migration source; the
Python coordinator owns migration execution and durable artifacts/history.

Apply the completed data model through these owner-scoped revisions. For legacy
models, retain source values, label inference or missing detail, and reconcile
business outputs on an isolated migrated copy before cutover. Further evidence
must come from actual retained sources; schema changes cannot reconstruct
uncaptured events. Back up the selected database before destructive data work;
include sidecars for SQLite.

### D. Publish narrow Business RPC tools

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

Lease the public business service with `toolframe_plugins.use_plugin(SERVICE_KEY)`
for the complete call, and translate its expected business failures into
`toolframe_runtime.ToolframeError`. Keep persistence inside the service: inject
`DATABASE` in plugin setup for bounded parameterized queries/data DML and
`database.run_once()` for complete replayable mutations. Inject `ARTIFACTS` when
the capability produces create-once large outputs.

The private
`_execute_sql_reference.py` demonstrates adapter mechanics but is not an MCP
capability and must never appear in peer docs. When a peer needs unsupported
behavior, implement a new narrow business tool through the master agent.

### E. Add the human decision surface

Create a direct public widget:

```sh
./toolframe scaffold widget <domain> <name>
```

A public `domains/<domain>/ui/<name>.py` exports synchronous `render(context)`.
Optional `TITLE` and integer `ORDER` control the registry; explicit order values
must be globally unique. `_*.py`, dot files, and nested directories are not
auto-loaded widgets.

Use Streamlit forms, tables, editors, metrics, and buttons to expose the
business loop. Lease the public business service through `context.plugins.lease`
for the entire render operation and submit business intent through its methods,
as MCP does. Use expected-version guards when stale writes matter.
Never accept arbitrary browser SQL.

For expected recoverable user conditions, create a
`DashboardInteractionError` and pass it to the single core
`render_error_card()`. The result must be one copyable card with a clear message,
recovery action, trace ID, and master-agent escalation. Do not build shared or
per-domain error card components. Programming errors are fatal and must not be
converted into a friendly local card.

### F. Publish only peer operating guidance

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

Run focused tests plus the repository's formatter/linter. Verify applicable
preservation rules on isolated data using the supported provider, recording
which provider and execution level were actually tested:

- Reject wrong-parent references even when both referenced rows exist; exercise
  duplicate business keys, meaningful numeric bounds and the chosen deletion rules.
- Reproduce the initial business view and an additional grouping or join from
  retained facts without reimporting. Include inputs with equal dashboard totals
  but different underlying detail so the extra query proves preservation.
- Where history matters, retain two distinct same-day occurrences while a retry
  stays idempotent, keep unknown dates unknown, and preserve issued values and
  sent content after corrections or template/contact edits.
- Rebuild any derived cache without changing authoritative records. If handoff or
  reprocessing is promised, reproduce it from retained inputs without the original
  machine, or name the unavailable input and the analysis it blocks. Verify that
  deletion preserves evidence needed by surviving records under the agreed policy.

Then require:

1. `./toolframe check --format json` succeeds.
2. Real startup reaches readiness and applies pending migrations.
3. Representative local invocation or MCP calls prove reads, writes, replay,
   stale input, and error behavior when relevant.
4. The dashboard completes the human path through the same business service.
   Stop the concrete service, prove MCP/widget calls are blocked, restore it and
   verify automatic recovery plus unchanged durable data and idempotent receipts.
5. A peer starts from the tested LAN `/llms.txt`, registers same-origin `/mcp`,
   and lists the actual tools before using them.
6. One end-to-end business outcome and its durable evidence are visible.

Reconcile private documents, catalog/bindings, contracts, model/schema, MCP/UI, peer docs,
and tests in the same change. In the final report, lead with the business
outcome, then state migration/data effects, tool names, dashboard actions,
tested URLs, evidence, restart requirements, limits, and the next human action.
