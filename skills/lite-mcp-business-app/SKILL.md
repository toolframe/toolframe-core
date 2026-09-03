---
name: lite-mcp-business-app
description: Load when a user wants a coding agent to create, extend, operate, connect, diagnose, or judge the fit of a business-specific LAN app inside an editable lite-mcp distribution, including its SQLite migrations, MCP tools, Streamlit dashboard, llms.txt LAN-agent guide, peer-agent workflow, and nontechnical operating handbook.
---

# Lite MCP business app

Turn a business outcome into one small, named, inspectable application built on
lite-mcp that a nontechnical owner can operate and another agent can inherit.

## Non-negotiables

- Lead with the business loop: who submits what, who acts, what the owner sees,
  and what proves completion. Translate technical choices into their business
  consequence.
- Treat Lite MCP as the framework name. Give every business app its own short,
  recognizable name; keep the authoritative identity in `APP_CONTEXT.md` and
  project it consistently into the Dashboard and LAN agent documents.
- Perform repository and runtime legwork directly. Ask the owner only for a
  business decision that materially changes behavior, risk, or data meaning;
  choose and record safe technical defaults.
- Treat the executable folder's `README.md`, `./lite-mcp --help`, migrations,
  actual tool signatures, and public helper APIs as the current runtime
  contract. Reconcile contradictions instead of relying on this skill's memory.
- Keep one app centered on one bounded business workflow. Closely related item
  types may share it only when lifecycle, actors, and data rules remain coherent.
- Preserve user data and applied migration bytes. Express every durable schema
  change as the next contiguous migration owned by the primary development
  agent; never delegate schema administration to a peer workflow agent or MCP
  SQL call. Back up state before destructive data work.
- Treat actor names as attribution, not identity. This product is trusted-LAN,
  unauthenticated application code; a need for security boundaries triggers the
  dedicated-app escalation path.
- Keep `APP_CONTEXT.md`, `DOMAIN_MODEL.md`, `llms.txt`, and the linked `llms/`
  documents aligned with migrations, tools, dashboard behavior, and tests in
  the same change. The `llms` files are a curated LAN-facing projection, not a
  second source of business truth.
- Prefix user-owned business tables with `app_`; treat `_lite_mcp_*` tables as
  runtime-owned and outside user migrations or repair SQL.

## 1. Establish intent and fit

Locate the editable application root and inspect its local README, CLI help,
current migrations, tools, dashboard, database state, and git status when the
folder is version-controlled. Classify the request as one of:

- **Develop:** create or change business behavior.
- **Doctor:** diagnose or repair an existing app without broadening its scope.
- **Operate:** run it, connect a peer agent, back it up, or explain daily use.
- **Fit:** decide whether lite-mcp remains the right product shape.

Read [operations and limits](references/operations-handbook.md) for Doctor,
Operate, or Fit. Continue only after the application root, intended business
outcome, current app identity, data at risk, and any hard escalation trigger
are explicit. On the Develop path, follow the development handbook's naming
step whenever the app is still `Unnamed` or uses Lite MCP as its product name;
propose the name without waiting for the owner to ask. For a Fit request,
report the decision without implementing unless the user also requested a
change.

Sections 2–5 are the Develop path. For Doctor, Operate, or Fit, stay in the
requested operations workflow: do not create handoff documents, redesign the
domain, or change source merely because those later sections exist. A Doctor
repair may edit only the diagnosed source and affected documentation when the
user asked for repair; diagnosis alone is read-only.

## 2. Model the business before code

For Develop, read the [development handbook](references/development-handbook.md).
When work is claimed by scheduled or peer agents, also read the canonical
[Job domain model](references/job-domain-model.md). Do not load that model for
an unrelated register, reporting, or lookup app.

For Develop, create or reconcile `APP_CONTEXT.md` from
[the app-context template](assets/APP_CONTEXT.template.md) and
`DOMAIN_MODEL.md` from
[the domain-model template](assets/DOMAIN_MODEL.template.md). Use the owner's
domain words in the UI while recording any canonical internal term. Make
states, transitions, invariants, actor intentions, source ownership, error
meaning, and large-result handling explicit.

Advance only when every user or agent action maps to an allowed state change or
read operation, every business invariant has one authoritative definition, and
unresolved business choices are visible rather than buried in implementation.

## 3. Implement one vertical business loop

Build in dependency order: migration, private domain/storage helpers, a small
Business RPC surface, Streamlit widgets, then the progressive `llms.txt` agent
guide. Use the binary's scaffold commands where they apply; inspect their
generated file before filling it. Keep one published tool per public tool file
and place shared support behind private underscore-prefixed modules.

Split the Business RPC surface into commands and queries. A business command
completes one named intent or state transition; a business query answers one
bounded workflow question. Let one tool hide every read, write, invariant
check, event, and receipt needed for that outcome. Use domain inputs and return
business state, evidence, and next-action details. Keep table/resource CRUD,
generic field/status patching, and arbitrary storage filters out of the public
tool interface.

Make business commands parameterized, transactional where invariants span
rows, idempotent where retries or reruns can duplicate intent, and guarded by
versions or leases where stale writers matter. Return structured, actionable
errors with retryability and correlation details. Store large durable output as
managed artifacts and keep bounded summaries/references in SQLite.

In MCP tools, obtain database access from `lite_mcp_db.get_database()`; do not
open `sqlite3` connections or read `LITE_MCP_DB` in tool source. Prefer narrow
business tools. Keep the shipped `execute_sql` as a bounded data-only fallback
and adapter example, and allow a peer to use it only when the app-specific
capability guide explicitly documents that fallback.

Advance only when every public tool traces to a caller intent and observable
business outcome, and the migration, tool surface, dashboard actions, domain
docs, and tests describe the same loop with no orphaned action or state.

## 4. Prove the application

Run the strongest checks available for the change. The minimum development gate
is:

1. `./lite-mcp check --format json` succeeds.
2. Domain tests cover legal and illegal transitions plus concurrency,
   idempotency, and lease behavior when those concepts exist.
3. A real server start succeeds and applies pending migrations before tools are
   exercised against the configured database.
4. Local tool invocation against migrated state, or the equivalent MCP calls,
   proves representative reads, writes, replay, stale input, and error
   classification.
5. The Dashboard completes the human path.
6. A peer-LAN client starts from the tested advertised `/llms.txt` address,
   follows its relevant linked documents, registers the same-origin `/mcp`
   Streamable HTTP server through its own client, and proves the connection by
   listing the actual tools before calling them.
7. One end-to-end business item reaches its intended review or terminal state
   with history and result visible.

A passing preflight is evidence, not the entire acceptance test. On failure,
follow the
[Doctor workflow](references/operations-handbook.md#doctor-report-first-then-repair),
fix the reported source of truth, restart when required, and rerun every
affected gate.

## 5. Hand off in business language

Lead the final report with what the owner can now accomplish. Include app-local
doc paths, changed business behavior, data migration/backups, tool names,
Dashboard actions, the tested Dashboard, Agent guide, and MCP addresses, verification
evidence, restart rules, known limits, and the next human action. Leave technical
detail behind links unless it changes how the owner operates the app.

For Develop, finish only when every agreed human and peer-agent path works and a
future coding agent can continue safely from `APP_CONTEXT.md` and
`DOMAIN_MODEL.md`, while a peer LAN agent can discover the supported workflow
from `/llms.txt`. A developed app must no longer present `Unnamed` or Lite MCP
as its owner-facing app name. For Doctor, finish with an evidence-backed cause
and, only when repair was requested, a verified repair. For Operate, finish
when the requested run, connection, backup, or explanation outcome is proven.
For Fit, finish with a clear keep/escalate decision and the business reason;
do not invent implementation work.

## Conditional references

- Read [development-handbook.md](references/development-handbook.md) for any
  schema, tool, widget, `llms.txt`, or app-document change.
- Read [job-domain-model.md](references/job-domain-model.md) only for queued,
  scheduled, claimed, leased, retried, or human-reviewed agent work.
- Read [operations-handbook.md](references/operations-handbook.md) for startup,
  peer LAN connection, backup, Doctor, limitations, escalation, or FAQ.
- Read [product-decisions.md](references/product-decisions.md) only when
  reviewing or changing this skill's product policy. In a source checkout,
  also read the source-only `evals/cases.json` and update evaluation evidence
  when present; production auto-installation intentionally omits evaluation
  directories.
- Use the app-context and domain-model assets whenever creating or reconciling
  the required app-local handoff documents; adapt their placeholders to the
  user's domain.
- Use the
  [MCP-unavailable response asset](assets/MCP_UNAVAILABLE_ISSUE.template.md)
  whenever a scheduled workflow or peer agent cannot reach `/mcp` before its
  intended business action completes.

## Recognition-critical gotchas

- lite-mcp stores and exposes work; an external scheduler or human starts the
  peer agent. Model schedule definitions only when the business app truly owns
  recurrence.
- MCP tools, migrations, and dependency changes take effect on process startup.
  Dashboard source may rerun on browser interaction; this is not tool hot reload.
- Keep the shipped `execute_sql` as a trusted, data-only fallback and adapter
  reference. It must reject schema, PRAGMA, maintenance, and runtime-owned table
  changes. Give operational agents Business RPC commands and queries; permit
  the fallback only when the app-specific capability guide says so.
- The first tested private LAN address is for peers; `127.0.0.1` is only for the
  host machine. Use that same LAN host and port for `/llms.txt` and `/mcp`.
- Give a new peer agent the tested LAN `/llms.txt` URL first. That guide can
  tell the agent to register same-origin `/mcp`, but it cannot install an MCP
  server into every possible client or bypass client approval. A successful
  tool-list response, not reading the guide, proves the connection.
- An unreachable MCP endpoint pauses the integration before business state is
  known. Return a visible warning and one self-contained, copyable issue from
  the response asset; do not invent a Job transition or claim that the server
  is down when only peer reachability failed.
- Invalid migrations, tools, dependencies, widgets, logging, or Dashboard code
  make the application unhealthy. Surface and repair the cause; a partially
  loaded catalog is not a successful start.
- One Python process, one shared dependency environment, and SQLite's writer
  model are deliberate simplicity limits, not a distributed worker platform.
