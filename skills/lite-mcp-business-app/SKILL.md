---
name: lite-mcp-business-app
description: Load when a user wants a coding agent to create, extend, operate, connect, diagnose, or judge the fit of a business-specific LAN app inside an editable lite-mcp distribution, including its SQLite migrations, MCP tools, Streamlit dashboard, peer-agent workflow, and nontechnical operating handbook.
---

# Lite MCP business app

Turn a business outcome into one small, inspectable lite-mcp application that a
nontechnical owner can operate and another agent can inherit.

## Non-negotiables

- Lead with the business loop: who submits what, who acts, what the owner sees,
  and what proves completion. Translate technical choices into their business
  consequence.
- Perform repository and runtime legwork directly. Ask the owner only for a
  business decision that materially changes behavior, risk, or data meaning;
  choose and record safe technical defaults.
- Treat the executable folder's `README.md`, `./lite-mcp --help`, migrations,
  actual tool signatures, and public helper APIs as the current runtime
  contract. Reconcile contradictions instead of relying on this skill's memory.
- Keep one app centered on one bounded business workflow. Closely related item
  types may share it only when lifecycle, actors, and data rules remain coherent.
- Preserve user data and applied migration bytes. Express every durable schema
  change as the next contiguous migration and back up state before destructive
  data work.
- Treat actor names as attribution, not identity. This product is trusted-LAN,
  unauthenticated application code; a need for security boundaries triggers the
  dedicated-app escalation path.
- Keep `APP_CONTEXT.md` and `DOMAIN_MODEL.md` aligned with migrations, tools,
  dashboard behavior, and tests in the same change.
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
outcome, current state, data at risk, and any hard escalation trigger are
explicit. For a Fit request, report the decision without implementing unless
the user also requested a change.

Sections 2–5 are the Develop path. For Doctor, Operate, or Fit, stay in the
requested operations workflow: do not create handoff documents, redesign the
domain, or change source merely because those later sections exist. A Doctor
repair may edit only the diagnosed source and affected documentation when the
user asked for repair; diagnosis alone is read-only.

## 2. Model the business before code

For Develop, read the [development handbook](references/development-handbook.md).
When work is claimed by scheduled or peer agents, also read the canonical
[Job domain model](references/job-domain-model.md). Do not load that model for
an unrelated CRUD, reporting, or lookup app.

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

Build in dependency order: migration, private domain/storage helpers, narrow
named MCP tools, then Streamlit widgets. Use the binary's scaffold commands
where they apply; inspect their generated file before filling it. Keep one
published tool per public tool file and place shared support behind private
underscore-prefixed modules.

Make business mutations parameterized, transactional where invariants span
rows, idempotent where retries or reruns can duplicate intent, and guarded by
versions or leases where stale writers matter. Return structured, actionable
errors with retryability and correlation details. Store large durable output as
managed artifacts and keep bounded summaries/references in SQLite.

Advance only when the migration, tool surface, dashboard actions, domain docs,
and tests describe the same business loop with no orphaned action or state.

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
6. A peer-LAN client completes the MCP path using the tested advertised address.
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
Dashboard actions, the tested Dashboard and MCP addresses, verification
evidence, restart rules, known limits, and the next human action. Leave technical
detail behind links unless it changes how the owner operates the app.

For Develop, finish only when every agreed human and peer-agent path works and a
future coding agent can continue safely from `APP_CONTEXT.md` and
`DOMAIN_MODEL.md`. For Doctor, finish with an evidence-backed cause and, only
when repair was requested, a verified repair. For Operate, finish when the
requested run, connection, backup, or explanation outcome is proven. For Fit,
finish with a clear keep/escalate decision and the business reason; do not
invent implementation work.

## Conditional references

- Read [development-handbook.md](references/development-handbook.md) for any
  schema, tool, widget, or app-document change.
- Read [job-domain-model.md](references/job-domain-model.md) only for queued,
  scheduled, claimed, leased, retried, or human-reviewed agent work.
- Read [operations-handbook.md](references/operations-handbook.md) for startup,
  peer LAN connection, backup, Doctor, limitations, escalation, or FAQ.
- Read [product-decisions.md](references/product-decisions.md) and
  [evals/cases.json](evals/cases.json) only when reviewing or changing this
  skill's product policy; they are maintenance evidence, not normal
  app-development context. Update the evaluation evidence after a policy
  change.
- Use the two assets whenever creating or reconciling the required app-local
  handoff documents; adapt their placeholders to the user's domain.

## Recognition-critical gotchas

- lite-mcp stores and exposes work; an external scheduler or human starts the
  peer agent. Model schedule definitions only when the business app truly owns
  recurrence.
- MCP tools, migrations, and dependency changes take effect on process startup.
  Dashboard source may rerun on browser interaction; this is not tool hot reload.
- Keep the shipped `execute_sql` as a trusted development/reference tool unless
  the owner explicitly changes that product decision. Give operational agents
  narrow business tools instead of arbitrary SQL instructions.
- The first tested private LAN address is for peers; `127.0.0.1` is only for the
  host machine. Use the same LAN host and port with `/mcp` for MCP.
- Invalid migrations, tools, dependencies, widgets, logging, or Dashboard code
  make the application unhealthy. Surface and repair the cause; a partially
  loaded catalog is not a successful start.
- One Python process, one shared dependency environment, and SQLite's writer
  model are deliberate simplicity limits, not a distributed worker platform.
