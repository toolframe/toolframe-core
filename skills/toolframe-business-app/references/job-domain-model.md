# Scheduled-agent Job domain model

This is the canonical model for a toolframe app that hands scheduled work
between a human and one or more peer agents on a trusted LAN. It is a business
contract, not an implementation dump. An app may use different business names,
but a scheduled work desk should preserve the meanings and guarantees below.

## Boundary and runtime decisions

The external scheduler decides when an agent should poll. toolframe is the
durable hand-off layer: it stores the work, user instruction context, attempts,
progress, results, and reviews, and exposes narrow MCP tools plus a human
dashboard. It does not run a scheduler, worker queue, or recurring timer.

The current product contract is:

- Direct public files in `domains/<job_domain>/tools/` and `ui/` are discovered
  at startup. A tool, widget, or domain-load error fails the whole app with its
  reason. MVP has no MCP hot-reload or reload API; restart after editing.
- One uv-managed Python runtime and process serves FastAPI, Streamlit, and
  FastMCP. MCP is Streamable HTTP at `/mcp`; SSE and stdio are not supported.
- The app binds for LAN use without authentication. Treat every tool and
  dashboard mutation as trusted-code access. the configured database is SQLite by default or PostgreSQL.
  Read [plugins-and-data.md](plugins-and-data.md) for persistence contracts.
- Peer agents receive only narrow, parameterized business tools that enforce
  this model. The private `_execute_sql_reference.py` may guide master-agent
  implementation but is not published and never becomes a peer repair path.
- Tool and mutation failures expose a stable reason, a trace identifier, and
  whether retrying is safe. Detailed diagnostics remain in the local logs.

These decisions are derived from the product-owner Q5–Q18 and Q27–Q30
decisions. The scheduler remains outside the app even when a scheduler happens
to be the actor making an MCP request.

## Ubiquitous language

| Term | Meaning and ownership | Avoid / common misuse |
| --- | --- | --- |
| **Job** | A durable business work item visible to the owner. It has an objective, a lifecycle state, and one current instruction context. The Job is not an execution process. | Do not use `Task` as a synonym when it could mean an Attempt or a scheduler item. |
| **ContextVersion** | An immutable snapshot of the user’s instructions, input facts, constraints, and references used for one or more attempts. Editing context creates a new version; it never rewrites an old snapshot. | Not a mutable settings blob or the whole Job. |
| **Attempt** | One agent execution of a Job. It records who claimed it, which ContextVersion was read, technical outcome, and the lease fence. Attempts are history, not the Job’s business state. | Not a second name for Job/Task, and not a business status. |
| **Lease** | A time-bounded exclusive right for one Attempt to write progress or submit a result. A fresh opaque lease token fences off an expired or replaced owner. | Not authentication, a user role, or a permanent lock. |
| **Result** | An immutable business output produced by an Attempt. Small summaries can be stored in SQLite; larger output should use a managed artifact reference. A Result can await review. | Not a replacement for the Attempt or a mutable “latest output” field. |
| **Review** | A human or designated reviewer’s decision about a Result: accept it, request changes, or explicitly reject it as a business outcome. Review is not a technical retry decision. | Do not use it as a synonym for debugging or transport retry. |
| **Event** | An append-only record of a business transition or material action, including actor, old/new state, reason, and correlation information. It is the audit history, not a mutable status cache. | Not a runtime log line; runtime logs belong in JSONL logs. |
| **Actor** | A label attributing an action, such as `human:<name>`, `agent:<name>`, or `system:<component>`. It is not authentication or proof of identity; preserve request source separately in audit logs. `scheduler` may label the external caller, but it is not an in-app worker. | Do not treat an actor label as permission enforcement or an in-app scheduler. |
| **Pending** | A query result, not a stored state: a due Job in `ready` **or** `changes_requested` with no valid unexpired Lease. `blocked`, `draft`, `claimed`, `in_progress`, `review`, and terminal work are not pending. | Not every nonterminal Job. Claiming atomically moves either eligible state to `claimed`. |
| **Business failure** | An explicit decision that the requested outcome cannot or should not be completed. It enters `failed` only through a deliberate guarded transition. | Not an automatic mapping from a technical error. |
| **Technical failure** | An execution or infrastructure problem, classified as retryable or non-retryable. It is recorded on the Attempt. Exhausted technical recovery enters `blocked`, never `failed` automatically. | Not a business-state transition until a guard explicitly blocks or fails the Job. |

## Recommended database mapping

User-owned tables use their owning domain prefix and are created by that
domain's owner-scoped schema revisions. The examples below assume
the domain is named `job_desk`; names and optional columns can be adapted, but
the relationships and constraints below should remain recognizable. The
runtime-owned `_toolframe_*` tables are
internal and must not be edited by a user migration.

| Table | Required meaning and representative columns |
| --- | --- |
| `job_desk_jobs` | `id` (stable text or integer key), `title`, `objective`, `status`, `priority`, `available_at`, `context_version_id`, `current_attempt_id`, `lease_token_hash`, `fencing_value`, `lease_expires_at`, `max_attempts` (default `3`), `version`, `created_by_actor`, `created_at`, `updated_at`, and optional terminal reason/timestamp. `version` is the optimistic-concurrency value. |
| `job_desk_job_context_versions` | `id`, `job_id`, monotonically increasing `context_number`, `instructions`, structured input/reference data, `created_by_actor`, and `created_at`. Enforce uniqueness of `(job_id, context_number)` and never update content in place. |
| `job_desk_job_attempts` | `id`, `job_id`, `context_version_id`, `attempt_number`, `claim_source_status` (`ready` or `changes_requested`), `actor_label`, `lease_token_hash`, `fencing_value`, `lease_expires_at`, `started_at`, `last_heartbeat_at`, `finished_at`, technical outcome, error code/message summary, and `retryable`. An Attempt is closed before a result is submitted or when a lease is abandoned. |
| `job_desk_job_results` | `id`, `job_id`, `attempt_id`, `summary`, optional structured payload, optional managed `artifact_ref`, `created_by_actor`, and `created_at`. Results are append-only; do not overwrite a previous result. |
| `job_desk_job_reviews` | `id`, `job_id`, `result_id`, `reviewer_actor`, `decision` (`accepted`, `changes_requested`, or explicit business `rejected`), review note, and `created_at`. |
| `job_desk_job_events` | Append-only `id`/sequence, `job_id`, event name, `from_status`, `to_status`, actor label, attempt/context/result identifiers when relevant, reason/payload, idempotency operation id, and `occurred_at`. No update or delete path should exist in business tools. |

Use foreign keys where the chosen schema permits them, indexes for pending
selection (`status`, `available_at`, `priority`), and an index for each
history lookup (`job_id`, event/result/review time). Apply the development
handbook's [relationship rules](development-handbook.md#model-durable-facts-before-implementation):
when a row stores `job_id` alongside an Attempt, ContextVersion or Result
reference, enforce that the referenced record belongs to that same Job.
Dashboard counts and combined workbench rows derive from the retained context,
attempt, result, review and event records. Store timestamps in a
consistent UTC representation. Keep human-facing text separate from machine
state values. The presented lease token may be returned to the current
Attempt, but persist only its hash plus a fencing value; neither is an
authentication or authorization mechanism.

## Job state machine

The state machine is finite and explicit. A tool may add a domain-specific
state only after updating the local model, migration, tools, dashboard, and
tests together. Do not silently add a state because a technical error was
convenient to represent that way.

### States

| State | Meaning | Is terminal? |
| --- | --- | --- |
| `draft` | Being prepared; it is not offered to agents. | No |
| `ready` | Published and eligible for an agent claim when due. | No |
| `claimed` | An Attempt owns a valid Lease but has not started reporting work. | No |
| `in_progress` | The Lease owner is actively working and may report progress. | No |
| `blocked` | Work cannot currently proceed; a human or agent must resolve the stated blocker. | No |
| `review` | A Result exists and is waiting for a business decision. | No |
| `changes_requested` | A reviewer wants more or different work; it is eligible for a new claim when due. | No |
| `done` | A reviewer accepted a Result. | Yes |
| `failed` | A human or designated operator explicitly recorded a business failure. | Yes |
| `cancelled` | A human or designated operator intentionally stopped the Job. | Yes |

### Allowed transitions and guards

| From | Action/event | Guard | To |
| --- | --- | --- | --- |
| `draft` | `publish_job` | Objective and an initial ContextVersion are present. | `ready` |
| `draft` | `cancel_job` | A specific operator action records its actor label and reason; the label does not verify identity. | `cancelled` |
| `ready` | `claim_job` | Job is due, no valid Lease exists, and the atomic claim succeeds. | `claimed` |
| `ready` | `cancel_job` | A specific operator action records its actor label and reason; the label does not verify identity. | `cancelled` |
| `claimed` | `start_attempt` | Caller presents the current Attempt and unexpired Lease token. | `in_progress` |
| `claimed` | `release_attempt` | Current Lease owner releases it, or the system detects expiry; `claim_source_status` is `ready`. | `ready` |
| `claimed` | `release_attempt` | Current Lease owner releases it, or the system detects expiry; `claim_source_status` is `changes_requested`. | `changes_requested` |
| `claimed` | `block_job` | A specific operator/agent action records a concrete blocker, actor label, and reason, or the Attempt records a technical failure at `max_attempts` (default `3`). | `blocked` |
| `claimed` | `cancel_job` | A specific operator action records its actor label and reason; the label does not verify identity. | `cancelled` |
| `in_progress` | `record_progress` | Current Attempt and Lease are valid. State remains `in_progress`; append an Event. | `in_progress` |
| `in_progress` | `lease_expired` | The stored Lease is expired; close the Attempt and make it eligible again; `claim_source_status` is `ready`. | `ready` |
| `in_progress` | `lease_expired` | The stored Lease is expired; close the Attempt and make it eligible again; `claim_source_status` is `changes_requested`. | `changes_requested` |
| `in_progress` | `submit_result` | Current Lease is valid and a Result is durably appended. | `review` |
| `in_progress` | `block_job` | A concrete blocker and recovery owner/reason are recorded, or the Attempt records a technical failure at `max_attempts` (default `3`). | `blocked` |
| `in_progress` | `mark_business_failed` | A specific operator action explicitly records an unavoidable business outcome, actor label, and reason. | `failed` |
| `in_progress` | `cancel_job` | A specific operator action records its actor label and reason; the label does not verify identity. | `cancelled` |
| `blocked` | `unblock_job` | The blocker is resolved and the operator action records its actor label and offers work again. | `ready` |
| `blocked` | `mark_business_failed` | A specific operator action explicitly records business failure and reason; technical exhaustion alone is insufficient. | `failed` |
| `blocked` | `cancel_job` | A specific operator action records its actor label and reason; the label does not verify identity. | `cancelled` |
| `review` | `accept_result` | Reviewer identifies the Result and accepts the business outcome. | `done` |
| `review` | `request_changes` | Reviewer supplies actionable changes; if instructions change, append the new ContextVersion before or in this same transaction. | `changes_requested` |
| `review` | `reject_business_result` | Reviewer action explicitly records rejection as a business outcome, actor label, and reason. | `failed` |
| `review` | `cancel_job` | A specific operator action records its actor label and reason; the label does not verify identity. | `cancelled` |
| `changes_requested` | `claim_job` | Job is due, no valid Lease exists, and the atomic claim succeeds. If instructions changed, a new ContextVersion is appended before or with `request_changes`; the Attempt records `claim_source_status=changes_requested`. | `claimed` |
| `changes_requested` | `cancel_job` | A specific operator action records its actor label and reason; the label does not verify identity. | `cancelled` |

Terminal states have no outgoing transitions. A transition, its Event, and any
related Attempt/Result/Review write must commit in one transaction. A repeated
request with the same idempotency key returns the original outcome; it does not
append a second business action.

## Claims, leases, and stale writes

`claim_next_job` is a single short database write transaction. Conceptually it:

1. obtains the write lock;
2. requeues any eligible expired Lease (closing its Attempt and appending an
   expiry Event), preserving that Attempt’s `claim_source_status`;
3. selects one due `ready` **or** `changes_requested` Job whose technical
   attempt budget is not exhausted, in deterministic order: highest priority,
   earliest `available_at`, earliest creation time, then stable id;
4. creates an Attempt with the next attempt number, its source pending state,
   and a fresh opaque lease token/fencing value; and
5. updates the Job to `claimed`, stores the Attempt/token hash/fencing
   value/expiry, increments `version`, and commits the claim Event.

The transaction returns the Job plus its immutable ContextVersion, Attempt id,
lease token, and expiry. It must not hold the lock while the agent works. Two
concurrent claimers therefore receive different Jobs, or one receives no
pending Job; they cannot both own the same Lease.

Every lease-protected mutation includes the Job id, Attempt id, the hash of the
presented lease token, fencing value, and expected `version` in its update
predicate. A heartbeat or mutation after
expiry, after another agent reclaimed the Job, or with a stale version affects
zero rows and returns a `stale_lease`/`version_conflict` result. The old agent
must stop writing, re-read the Job, and claim again if appropriate. Retrying
the same stale write blindly is not safe and should be reported as a
non-retryable conflict; a fresh claim is the recovery action.

Lease expiry is not a business failure. It closes or marks the Attempt as
abandoned, appends history, and returns the Job to the same due-pending pool
(`ready` or `changes_requested`) recorded in `claim_source_status`. A later
Attempt may use the same ContextVersion unless a human creates a newer one.

## Retry, idempotency, and optimistic concurrency

Transport and tool errors have their own `retryable` classification. A
retryable timeout or database busy error may be retried with the same
idempotency key. Syntax, validation, constraint, policy, stale-lease, and
idempotency-input-mismatch errors are not safe blind retries. Include the trace
id and recovery hint in the tool response; keep full diagnostics in the local
JSONL logs.

Technical failure is recorded on the Attempt, not converted to `failed`. The
default technical attempt budget is **3** per Job (`max_attempts`); a business
override must be explicit in the app model. A retryable technical failure may
lead to another Attempt while the budget remains. Once technical recovery is
exhausted or needs human intervention, transition the Job to `blocked` with
the error code, attempt history, owner, and next action. Never transition to
`failed` automatically because the budget was exhausted.
`failed` requires an explicit business decision.

Every mutation tool should accept a caller-provided `idempotency_key`. MCP
tools wrap the complete business mutation, Event, and successful receipt with
`toolframe_idempotency.run_once(...)`; Dashboard code uses
`context.db.run_once(...)`. Do not nest either helper inside another
transaction. Reusing a key with different canonical input is a non-retryable
mismatch; a replay must have the same value shape as the first call. A fresh
poll uses a fresh key: retrying an earlier successful “no pending Job” poll
correctly replays the same empty result rather than claiming later work.

Use `version` as an optimistic fence for dashboard edits and review actions.
An update with an old version must make no change and show a conflict asking
the person to refresh. Do not silently overwrite a peer LAN agent or human.

## Business RPC tool contracts

Keep this as a small command/query catalog, not one CRUD interface per Job
table. Names may be adapted to the business vocabulary, but each tool completes
one business intent or answers one bounded workflow question and returns
structured business data:

- `create_job` — create a draft with its initial ContextVersion.
- `publish_job` — validate and make a draft ready.
- `list_pending_jobs` — read due `ready` or `changes_requested` Jobs; never
  reserve them.
- `claim_next_job` — atomically claim one Job and return context, Attempt, and
  Lease details.
- `get_job_context` — read the exact immutable ContextVersion for an Attempt.
- `start_job_attempt` — move the current valid Lease from `claimed` to
  `in_progress`.
- `renew_job_lease` — extend a valid Lease without creating business progress;
  use it when work lasts longer than the progress cadence.
- `record_progress` — append progress/Event data under a valid Lease.
- `submit_result` — append a Result and move the Job to `review`.
- `report_job_blocked` — record a blocker and release the active Lease into
  `blocked`.
- `list_jobs_needing_attention`/`get_job_workbench` — provide bounded status,
  context, progress, result, and history views for a concrete decision.
- `review_job` — accept, request changes, or explicitly reject a Result.
- `unblock_job`, `cancel_job`, and `mark_business_failed` — guarded operator
  actions with a required reason.

All command tools validate business input, use bound parameters, atomically
enforce the transition, and return the new state/version, Event/operation
identifiers, evidence, and next allowed action. Query tools return only the
bounded business view needed by their named purpose. No generic SQL, schema,
repair, or raw CRUD capability belongs in the peer-facing catalog.

## Dashboard responsibilities

The Streamlit dashboard is the human entrance: show counts and filters, pending
and blocked work, the current ContextVersion, Attempt/progress history, Results,
and review controls. Forms should create/edit drafts, publish, add a human
progress note, cancel, unblock, and review through the same parameterized
domain rules and idempotency guarantees as MCP tools. A stale form must show a
conflict and offer refresh. A widget catches expected typed errors and displays
the actionable message; it does not expose arbitrary SQL input.

Dashboard source files may rerun after edits, but MCP tools are still startup
only. If the dashboard cannot load, the whole app fails startup so the owner
never sees a misleading partial control surface.

## Concurrency and contract tests

Before calling the app complete, test at least:

- two concurrent claimers competing for one due Job;
- a lease expiry followed by a new claim and a stale old-agent write;
- concurrent first calls with the same idempotency key and a same-key/different-
  input mismatch;
- dashboard and MCP updates racing on the same optimistic `version`;
- immutable ContextVersion reads after a newer ContextVersion is created;
- append-only Event, Result, and Review history;
- every allowed transition and rejection of every forbidden transition;
- retryable database-busy/transport errors versus non-retryable validation and
  business conflicts; and
- the complete create → publish → claim → progress → result → review → done
  loop through `check`, tool invocation/MCP, and the dashboard.

Keep the scheduler out of these tests: use a test actor to simulate its poll.

## Source-of-truth reconciliation

Maintain `APP_CONTEXT.md` for the app’s purpose, actors, operating loop, and
limits, and `DOMAIN_MODEL.md` for the app-specific adaptation of this model.
Domain schema manifests declare immutable revisions and explicit dependencies.
Add a revision for changes and preserve the original legacy history.
Tool signatures are the callable contract, dashboard widgets are the human projection, and `./toolframe check
--format json`, `/info`, and local JSONL logs are runtime evidence.

When these sources disagree, stop and reconcile the docs, schema, tools,
dashboard, and tests in one change. Do not repair a contradiction by changing
the applied migration ledger, adding a hidden status, or teaching one caller a
private exception.
