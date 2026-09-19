---
name: toolframe-business-app
description: Load when a user wants a coding agent to create, extend, operate, connect, diagnose, or judge the fit of a business-specific LAN app inside an editable Toolframe distribution, including domain-owned SQLite migrations, MCP tools, Streamlit widgets, the authored llms.txt peer guide, and master-agent handoff documents.
---

# Toolframe business app

Turn a business outcome into one small, named, inspectable Toolframe app that a
nontechnical owner can operate and a peer agent can use through narrow MCP
business capabilities.

## Establish the agent role

- The **master development agent** works in the app folder. It may edit source,
  create migrations, add capabilities, diagnose failures, and reconcile the
  private handoff documents.
- A **peer agent** starts from `/llms.txt` and uses only the published MCP
  business tools needed for its assigned workflow. It must not administer the
  schema, expose raw SQL, repair source, infer private implementation details,
  or treat a missing capability as permission to alter the app.
- When acting as a peer, preserve the exact URL, tool name, error, and trace ID,
  then route missing capabilities or defects to the master development agent.

Do not blur these roles. `APP_CONTEXT.md` and `DOMAIN_MODEL.md` are private
master-agent sources; `app/llms.txt` and its explicitly linked domain documents
are the public peer-agent contract.

## Non-negotiable source contract

Work only in this layout:

```text
APP_CONTEXT.md
DOMAIN_MODEL.md
app/{dashboard.py,llms.txt,pyproject.toml,plugins.py}
domains/<business_domain>/
  __init__.py
  llms/
  migrations/
  model/
  tools/
  ui/
```

- Do not recreate legacy root `dashboard/`, `tools/`, `migrations/`, or `llms/`
  trees and do not create a default `shared/` package.
- Develop a complete business capability as one meaningful service plugin within
  its owning domain. Keep entities, validation and ordinary functions in `model/`;
  a small edit extends that capability rather than creating another plugin.
  Local capabilities can stay in the app; separate packages are a distribution choice.
- Keep one cohesive business capability in one domain. Use lower snake case for
  domain, tool, widget, and migration-description names.
- Domain code may import its own internals. Across domains, import only explicit
  business service keys/contracts or lease-owning business functions from the
  provider's root `__init__.py`. Declare `requires` for services retained by a
  plugin and hold a lease for call-time access. Deep model/storage imports are invalid.
- `app/dashboard.py` is the unrestricted application composition root. The
  default registry is convenient, not mandatory.
- Preserve user data and the exact bytes of applied migrations. Domain-owned
  revisions use stable owner namespaces; preserve the original legacy history.
- Prefix business tables with the owning domain name. `_toolframe_*` is
  runtime-owned.
- Public tools and widgets are direct `*.py` files. `_*.py`, dot files, and
  nested directories are private/not discovered.
- One public tool file contains one synchronous decorated function. Filename,
  function name, and globally unique MCP name must match.
- Never publish generic SQL, schema, repair, or raw CRUD capabilities. The
  private `_execute_sql_reference.py` is implementation reference code only.
- Use one core `render_error_card()` for expected recoverable widget errors.
  Do not add shared/domain error-card variants. Programming defects propagate
  and make startup or the running app fail.
- Treat every public migration, dependency, domain API, tool, and widget as
  required. A partial catalog is never an acceptable start.

## 1. Establish intent and fit

Identify the person or agent who submits work, who acts, what the owner sees,
what durable evidence proves completion, and the one screen or query needed
next. Translate implementation choices into business consequences; ask the
owner only when a choice changes behavior, risk, or data meaning.

Use Toolframe for one trusted LAN, one bounded workflow, one selected SQLite or PostgreSQL database,
short-lived interactions, and trusted Python. Read
[operations-handbook.md](references/operations-handbook.md) and recommend a
dedicated app when the request needs public access, authentication,
authorization, tenants, untrusted code, high availability, durable workers,
heavy concurrent writes, multiple databases, or formal compliance controls.

Toolframe is the framework name. Give the business app its own short name.
When the scaffold still says `Unnamed`, propose two or three names with one
recommendation and apply the chosen or clearly provisional name consistently.

## 2. Inspect before editing

From the executable root, inspect git status, `README.md`, `./toolframe --help`,
`APP_CONTEXT.md`, `DOMAIN_MODEL.md`, `app/`, every relevant domain, and the
current check report:

```sh
./toolframe check --format json
```

Reconcile contradictions against the executable's actual CLI, migrations,
tool signatures, widget contracts, and helper APIs. Do not assume this skill's
copy is newer than the app.

Classify the request as Develop, Doctor, Operate, Connect, or Fit. Diagnosis is
read-only unless repair was requested. Operation does not authorize source
redesign. A peer connection request does not authorize adding tools.

## 3. Model before code

For Develop, read
[development-handbook.md](references/development-handbook.md) and
[plugins-and-data.md](references/plugins-and-data.md), including when no schema
change is needed. Reconcile
`APP_CONTEXT.md` with the app-context asset and `DOMAIN_MODEL.md` with the
domain-model asset. Keep the owner's terms, entities, invariants, states,
actors, source ownership, and error meaning explicit.

Before implementation, record each table's grain (what one row represents),
identity, relationship rules and recoverable inputs for derived views. For
history, distinguish observed events from inference and state which past values
survive correction or deletion. Complete the applicable decisions in the
handbook's [data-model gate](references/development-handbook.md#model-durable-facts-before-implementation)
before writing migration SQL; expose unavailable detail rather than inventing it.

Read [job-domain-model.md](references/job-domain-model.md) only when the app
owns queued, claimed, leased, retried, or human-reviewed agent work. Do not
force the Job vocabulary onto an ordinary register, lookup, or report.

Choose the domain that owns the behavior. Create a new domain only when its
language, invariants, and lifecycle form a real bounded business capability:

```sh
./toolframe scaffold domain <business_domain>
```

## 4. Connect one business plugin end to end

Build in this order:

1. Define the domain's business service contract and stable `ServiceKey`. Record
   its concrete Plugin ID, schema owner and required services. Finish when the
   interface expresses the owner's complete business intent.
2. Implement that service in `model/`, keeping authoritative rules and storage
   there. Define one plugin entry, normally `model/plugin.py`, with `provides`,
   `requires`, validated settings and owned-resource setup/cleanup as needed.
3. Add an owner-scoped schema revision if needed; require `DATABASE` so host
   migrations complete before business setup. Keep revision dependencies separate.
4. Add needed packages to `app/pyproject.toml`, register the definition in
   `app/plugins.py`, and bind the domain with `DOMAIN_PLUGINS`. The host already
   creates `domain.<name>`; connect that gate to the business plugin instead of
   defining a duplicate or disconnected empty plugin.
5. Make narrow MCP tools and widgets lease the same business service. Keep the
   domain's tools, UI and peer guide together; test their common business outcome.
6. Reconcile private handoff docs and peer-facing changes. Finish the slice only
   after startup, stop/blocked/recovery and retained-data checks pass.

Follow the copyable [Stockroom example](assets/business-plugin/README.md) for the
full assembly. Scaffolds create source skeletons; the developer completes and
registers the real business service before calling the capability finished.

Use the domain-aware scaffolds:

```sh
./toolframe scaffold migration <domain> <description>
./toolframe scaffold tool <domain> <name>
./toolframe scaffold widget <domain> <name>
```

Business commands complete one guarded intent or legal state transition.
Business queries answer one bounded workflow question. Hide SQL, invariants,
events, version/lease checks, idempotency, and transaction details behind that
small interface.

Inject the host `DATABASE` service into persistence-owning business plugins and
keep replay-safe mutations in its `run_once()` transaction. Public tools lease
business services and use `ToolframeError` for structured failures; widgets use
the same service and the core error card. See plugins-and-data for host helpers.

## 5. Keep document audiences separate

- `APP_CONTEXT.md`: master-agent purpose, actors, boundaries, decisions,
  operating state, source map, and handoff evidence.
- `DOMAIN_MODEL.md`: master-agent ubiquitous language, entities, invariants,
  transitions, concurrency, and storage mapping.
- `app/llms.txt`: concise authored peer entry point with the app identity,
  same-origin `/mcp`, connection proof, and links to only the relevant domain
  documents.
- `domains/<domain>/llms/*.md`: focused peer instructions for supported
  capabilities and workflows. They are served only when explicitly linked as
  `/llms/<domain>/<name>.md` from `app/llms.txt`.

Toolframe must not generate a catalog into `app/llms.txt`. Do not publish the
private master documents, source repair steps, internal error catalogs, raw
database operations, or unavailable tools to peers. State that missing app
behavior must go through the master development agent.

## 6. Prove the complete app

At minimum:

1. Run focused model, data-preservation, plugin, migration, MCP/UI and error-path
   tests.
2. Run `./toolframe check --format json` and require `ok: true`.
3. Start the real server and confirm it reaches readiness with every domain,
   migration, tool, widget, dependency, and linked LLMS document intact.
4. Invoke representative tools against migrated state, including replay,
   stale-input, and error behavior when applicable.
5. Complete the human path through the same service. Stop its concrete plugin,
   verify tools/widgets are blocked, restore it, and prove data/receipts remain.
6. Give a peer the tested LAN `/llms.txt`, have it register same-origin `/mcp`
   through its client, and prove the connection by listing actual tools.
7. Complete one end-to-end business outcome with its durable evidence visible.

On failure, do not skip the broken domain or weaken validation. Preserve the
full stderr/check error, fix the source of truth through the master agent,
restart when required, and rerun affected gates.

## 7. Hand off in business language

Lead with what the owner can now accomplish. Include the app name, changed
business behavior, migration/data effect, published tool names, dashboard
actions, peer guide URL, verification evidence, restart requirements, known
limits, and the next human action.

Finish Develop work only when `APP_CONTEXT.md`, `DOMAIN_MODEL.md`, catalog/bindings,
service contracts, models/schema, tools, UI, peer docs and tests agree. Finish a
Doctor repair only after the cause and requested fix are verified. Finish a
Connect task only after actual tool listing proves MCP registration.

## Conditional references

- Read [development-handbook.md](references/development-handbook.md) for any
  source or document change.
- Read [plugins-and-data.md](references/plugins-and-data.md) for Develop work,
  service recovery/configuration, schema/provider changes or target selection.
- Read [operations-handbook.md](references/operations-handbook.md) for startup,
  LAN connection, backup, Doctor, failure, limits, or FAQ.
- Read [job-domain-model.md](references/job-domain-model.md) only for owned Job
  queue/claim/lease/review behavior.
- Read [product-decisions.md](references/product-decisions.md) only when
  reviewing or changing this skill's policy. In a source checkout, also update
  evaluation cases/evidence; production installation omits `evals/`.
- Use `assets/APP_CONTEXT.template.md` and
  `assets/DOMAIN_MODEL.template.md` when creating private handoff documents.
- Use `assets/ARTIFACT_TOOL.template.py` only as a private implementation
  reference for a narrow artifact-producing business tool.
- Use `assets/MCP_UNAVAILABLE_ISSUE.template.md` when a peer cannot reach MCP
  before its intended business action completes.

## Recognition-critical gotchas

- Migrations, MCP tools, dependencies, and the linked LLMS allowlist are
  startup contracts. Dashboard code may rerun in Streamlit; this is not MCP hot
  reload.
- New schema revisions belong to their owner; existing legacy numbers and bytes stay unchanged.
- A domain root `__init__.py` is a deliberate business boundary, not a dumping
  ground for models or storage helpers.
- Reading `/llms.txt` does not install MCP. A successful tool-list response is
  the connection proof.
- `127.0.0.1` works only on the host. Peers use the printed LAN host and the
  same port for `/llms.txt` and `/mcp`.
- One process and one dependency environment remain the runtime limits.
  Provider choice changes database behavior; it does not add worker isolation.
