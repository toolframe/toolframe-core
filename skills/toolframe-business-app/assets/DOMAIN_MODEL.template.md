# Domain model: `<business name>`

This private document is the durable hand-off for the master development agent.
It uses the business’s own language and records the smallest model that makes
the workflow unambiguous. It is not served to peers. Replace the placeholders;
remove sections that truly do not apply. Do not leave two competing definitions
in code and documentation.

## Purpose and boundary

- Business outcome: `<what becomes easier or more reliable>`
- In scope: `<records, people, decisions, and actions this app owns>`
- Outside the app: `<scheduler, external systems, manual processes>`
- Human entrance: `<dashboard pages/widgets>`
- Agent entrance: `<MCP tools and `/mcp` connection>`
- Data source and database: `<selected provider/target and any external read-only references>`
- Input-retention owner: `<this app or named upstream; snapshot/access/backup needed at handoff>`
- Future questions supported / unavailable: `<what retained detail enables; known limits>`
- Owning source domain(s): `<domains/<business_domain>>`
- Public business service: `<Protocol/ServiceKey exported from domain __init__.py>`
- Concrete plugin / schema owner / host domain gate: `<distinct identities and app/plugins.py binding>`
- Cross-domain consumers: `<public contract, business reason, requires/lease ownership, or none>`

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

| Entity | Grain: one record means | Identity | Required fields | Lifecycle owner |
| --- | --- | --- | --- | --- |
| `<entity>` | `<one organization / enrollment / order / occurrence>` | `<stable key; source identity; ambiguous-match policy>` | `<fields>` | `<human / agent / system>` |

Apply the bundled skill's data-model rules before writing a migration.
Record each relationship's integrity and lifecycle, including any same-parent
rule that separate foreign keys cannot establish.

| Relationship | Multiplicity and optionality | Keys / uniqueness / link table | Update/delete rule | Enforcement |
| --- | --- | --- | --- | --- |
| `<parent → child>` | `<1:1 / 1:many / many:many; required or optional>` | `<foreign key, unique key, junction if needed>` | `<restrict / cascade / null / archive, with reason>` | `<supported constraint or owning service's guarded transaction; test>` |

Rules that must remain true across every write:

1. `<invariant one>`
2. `<invariant two>`
3. `<invariant three>`

## Database mapping

User tables use the owning domain's prefix, such as `<business_domain>_`. Files
under the owner's `migrations/` declare schema revisions and provider SQL variants.
Applied revisions and legacy history stay immutable; append an explicit dependency
and new target for upgrades. `_toolframe_*` tables belong to the Python runtime.

| Table | Columns and constraints | Indexes | Created/changed by |
| --- | --- | --- | --- |
| `<business_domain>_<entity>` | `<columns, nullability, checks, foreign keys, version>` | `<business query indexes>` | `<owner@revision>` |
| `<business_domain>_<entity>_events` | `<append-only event columns>` | `<history indexes>` | `<owner@revision>` |

### Facts, sources, and derived views

Use the handbook's data-model rules to distinguish authoritative records,
presentation and caches. Keep service/schema ownership above in force for all
retained inputs and derived data.

| Stored data | Role | Source / identity / transformation | Retention or historical meaning |
| --- | --- | --- | --- |
| `<table or artifact>` | `<observed fact / inferred state / source copy / issued snapshot>` | `<source ID, supported subject/relationships, unresolved associations, import time, input/classification/rule revisions>` | `<retained values per revision; deletion policy; unavailable detail>` |

| Business view or derived cache | Grain | Authoritative inputs and calculation | Refresh / rebuild |
| --- | --- | --- | --- |
| `<dashboard card / query / cache>` | `<one output row means>` | `<recoverable tables/snapshots; calculation; overlapping-membership rule>` | `<query at read time, or justified cache owner/freshness/rebuild>` |

For activity or history, record the applicable time and evidence semantics:

| Record | Occurrence identity and retry key | Time meaning | Evidence and history |
| --- | --- | --- | --- |
| `<interaction / observation / issued report>` | `<one actual occurrence; distinct same-day records allowed>` | `<occurred at and precision/unknown; recorded at>` | `<observed/inferred; past payload retained; surviving evidence after deletion>` |

For upstream inputs, record the retained snapshot/version, owner, retrieval
location and backup/handoff coverage. A version fence protects a write; state
separately where historical values are retained. Cross-domain inputs use the
provider's public business service and an explicit retention agreement.

The model is ready for implementation when every authoritative table has a
declared grain and identity, relationships have integrity and lifecycle rules,
and derived views trace to recoverable inputs. Record the consequence of
inferred or unavailable detail and one additional grouping or join the retained
facts support beyond the initial dashboard.

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

Dashboard widgets and MCP tools lease the same business service, which owns the
model rules and parameterized database operations. Both entrances must show
validation, conflict, and retryable errors clearly; they
must not accept arbitrary SQL from a browser. The app’s MCP endpoint is
`/mcp`; confirm the printed LAN URL before handing it to a peer agent.

For expected recoverable widget errors, use the one core-owned error card.
Programming failures remain fatal. Do not model repair/schema tools as peer
capabilities; route missing behavior to the master development agent.

## Tests and completion evidence

Trace every published tool to a caller intent and observable business outcome.
Test each allowed and forbidden transition, concurrent claims or edits, lease
expiry/stale writes, idempotent replay and key mismatch, immutable context,
append-only history, and the complete business loop. Verify that public tools
cannot be composed to bypass an invariant. Then run
`./toolframe check --format json`, a fresh startup, dashboard interaction, and
an MCP/tool call. Record the date, command, and observed evidence here:

- Migration/check evidence: `<...>`
- Data-preservation evidence: `<provider actually exercised; wrong-parent links/duplicates/deletion; original view and additional query; applicable history, unknown-date, retry, revision and input-recovery checks>`
- MCP evidence: `<...>`
- Dashboard evidence: `<...>`
- LAN peer evidence: `<...>`

## Change and reconciliation record

When the business changes, update this document, `APP_CONTEXT.md`, the next
owner-scoped schema revision, affected model, tools, widgets, linked peer
documents, and tests together. Never edit an applied migration or hide a
contradiction in one caller.

| Date | Decision/change | Files or migration | Owner |
| --- | --- | --- | --- |
| `<YYYY-MM-DD>` | `<decision>` | `<paths>` | `<actor>` |
