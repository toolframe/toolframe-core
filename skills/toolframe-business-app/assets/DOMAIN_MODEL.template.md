# Domain model: `<business name>`

This document is the durable hand-off for the next human or coding agent. It
uses the business’s own language and records the smallest model that makes the
workflow unambiguous. Replace the placeholders; remove sections that truly do
not apply. Do not leave two competing definitions in code and documentation.

## Purpose and boundary

- Business outcome: `<what becomes easier or more reliable>`
- In scope: `<records, people, decisions, and actions this app owns>`
- Outside the app: `<scheduler, external systems, manual processes>`
- Human entrance: `<dashboard pages/widgets>`
- Agent entrance: `<MCP tools and `/mcp` connection>`
- Data source and database: `<data/data.db and any external read-only references>`

## Ubiquitous language

| Term | Plain-language meaning | Stored as / owner | Avoid / common misuse |
| --- | --- | --- | --- |
| `<Entity>` | `<what it means to the business>` | `<table/key; which actor may change it>` | `<ambiguous synonym or nearby concept>` |
| `<Context or instruction>` | `<facts/rules needed to act>` | `<immutable/versioned or mutable, with reason>` | `<what must not be confused with context>` |
| `<Attempt or activity>` | `<one execution or handling instance>` | `<table/key and technical outcome>` | `<a different entity or business state>` |
| `<Result or output>` | `<business output awaiting use/review>` | `<table/artifact reference>` | `<status, log line, or mutable latest value>` |
| `<Review or decision>` | `<who decides what happens next>` | `<table/allowed decisions>` | `<technical retry or authentication>` |
| `<Event>` | `<append-only history fact>` | `<event table and actor>` | `<runtime log or mutable status cache>` |
| `<Actor>` | `<label attributing who the caller says performed the action; it does not verify identity>` | `<stable label; request metadata is separate>` | `<authentication, permission, or proof of identity>` |

If a term is not used by the owner, choose a clearer business term. Keep
technical terms in the “stored as” column rather than making the owner learn
them.

## Entities and relationships

Describe each entity in one sentence, then record its identity and lifecycle.

| Entity | Identity | Required fields | Relationships | Lifecycle owner |
| --- | --- | --- | --- | --- |
| `<entity>` | `<stable id>` | `<fields>` | `<belongs to / references>` | `<human / agent / system>` |

Rules that must remain true across every write:

1. `<invariant one>`
2. `<invariant two>`
3. `<invariant three>`

## SQLite mapping

User tables use the `app_` prefix. The migration directory is the schema source
of truth. Applied migrations are immutable; add the next contiguous migration
for a change. Keep launcher-owned `_toolframe_migrations` and
`_toolframe_idempotency` tables out of user migrations.

| Table | Columns and constraints | Indexes | Created/changed by |
| --- | --- | --- | --- |
| `app_<entity>` | `<columns, nullability, checks, foreign keys, version>` | `<business query indexes>` | `<migration number>` |
| `app_<entity>_events` | `<append-only event columns>` | `<history indexes>` | `<migration number>` |

Use UTC timestamps, bound parameters, and short transactions. Large outputs
belong in managed artifact references rather than oversized JSON columns.

## State model

State is a finite business lifecycle, not a list of error messages. Define the
states and terminal states first.

| State | Meaning to the business | Terminal? |
| --- | --- | --- |
| `<state>` | `<plain-language meaning>` | `<yes/no>` |

| From | Action/event | Guard and required reason | To |
| --- | --- | --- | --- |
| `<state>` | `<action>` | `<who may do it; data/lease/version condition>` | `<state>` |

Reject every transition not in this table. Technical retryability is separate
from business state; record it on the relevant attempt/event. If work can be
claimed by peers, define an expiring lease and an opaque fencing token, and
reject stale writes after expiry or replacement.

## Invariants and concurrency rules

- Identity/uniqueness: `<what cannot be duplicated>`
- Version fence: `<which writes require expected version>`
- Lease fence: `<which actor/token may write during an attempt>`
- Context history: `<what is immutable and how a new version is made>`
- Append-only history: `<events/results/reviews that are never overwritten>`
- Idempotency: `<mutation namespace/key/input and replay behavior>`
- Retryability: `<safe transport/technical retries; unsafe conflicts>`
- Privacy/trust boundary: `<LAN/no-auth data exposure and allowed actors>`

## MCP and dashboard contract

Define the smallest Business RPC catalog that completes the business loop.
Separate bounded business queries from guarded business commands. A query
answers one workflow question; a command completes one intent or legal state
transition and hides every required read, write, invariant, event, receipt, and
transaction. Do not mirror tables with create/read/update/delete tools or
expose generic field/status patching and arbitrary storage filters.

| Tool | Kind | Caller intent | Business inputs | Outcome/evidence | Guard, failure, and recovery |
| --- | --- | --- | --- | --- | --- |
| `<intent-shaped name>` | `<query/command>` | `<decision or result sought>` | `<domain values, version/lease/idempotency when needed>` | `<state/view/version/event/next action>` | `<precondition, code, retryable?, recovery>` |

Dashboard widgets project the same model for humans and use parameterized
helpers. They must show validation, conflict, and retryable errors clearly; they
must not accept arbitrary SQL from a browser. The app’s MCP endpoint is
`/mcp`; confirm the printed LAN URL before handing it to a peer agent.

## Tests and completion evidence

Trace every published tool to a caller intent and observable business outcome.
Test each allowed and forbidden transition, concurrent claims or edits, lease
expiry/stale writes, idempotent replay and key mismatch, immutable context,
append-only history, and the complete business loop. Verify that public tools
cannot be composed to bypass an invariant. Then run
`./toolframe check --format json`, a fresh startup, dashboard interaction, and
an MCP/tool call. Record the date, command, and observed evidence here:

- Migration/check evidence: `<...>`
- MCP evidence: `<...>`
- Dashboard evidence: `<...>`
- LAN peer evidence: `<...>`

## Change and reconciliation record

When the business changes, update this document, `APP_CONTEXT.md`, the next
migration, affected tools, widgets, and tests together. Never edit an applied
migration or hide a contradiction in one caller.

| Date | Decision/change | Files or migration | Owner |
| --- | --- | --- | --- |
| `<YYYY-MM-DD>` | `<decision>` | `<paths>` | `<actor>` |
