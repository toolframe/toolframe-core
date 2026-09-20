# Business design

Use the user's vocabulary to define the smallest useful business capability.
Toolframe's plugin boundary is a deployable package and a data owner; it need not
be a folder per screen or a separate plugin for every entity.

## From workflow to contract

Record these decisions in the plugin's own design notes, at a level proportional
to the request. Keep the notes outside the files published by `llms.document`.

| Decision | Evidence needed before implementation |
| --- | --- |
| Outcome and actors | Who performs which operation, and what successful completion means. |
| Vocabulary and identity | Stable business identities and the meaning of statuses, units, dates and money. |
| Invariants | Rules that must hold even when a page, MCP tool and another plugin call concurrently. |
| Data ownership | Which plugin owns each record; which services expose it to other plugins. |
| Commands and queries | Inputs, outputs, validation failures, conflict behavior and required read/write permissions. |
| Presentation | Required pages/tools/docs and the behavior when those optional providers are absent. |
| Persistence and change | Constraints, indexes, transaction boundaries and whether existing data needs migration. |

For example, inventory requires an explicit choice between an editable current
balance and an append-only movement history. If corrections must be explainable,
model receipts, issues and reversals, and derive stock from movements. Decide
whether negative stock is allowed and check it in the same transaction as a new
movement. A table with an unrestricted editable quantity does not establish that
rule. Choose according to the actual workflow, not this example by default.

Use stable IDs for references, not display names. Define timezone and date-only
semantics. For money, choose a currency and exact representation such as integer
minor units; an unrestricted binary float is not an accounting contract. Add
uniqueness and database constraints for rules that persist independently of UI.

## Concurrency and side effects

Enforce invariants in the business service. The page can explain a validation
failure but must not be its only enforcement point. For editable records, use a
version or another explicit conflict strategy; a stale editor should not silently
replace a newer write. Keep related same-owner changes in one managed transaction.

Other plugins consume a declared, versioned service rather than opening this
plugin's SQLite file. There is no cross-owner SQL transaction. If a workflow
changes multiple owners, define its intermediate states and reconciliation.

Storage mutation receipts can help resolve an uncertain database write. They do
not make an entire business method or external HTTP side effect exactly-once.
Define an application-level idempotency key when safe retries are a requirement;
otherwise surface an unknown outcome for reconciliation instead of replaying it.

## Scope and completion

Ask only about choices that change behavior and cannot be inferred from the
request or existing source. Record reversible defaults and continue independent
work. Design-only completion is a reviewable model and contract, including any
open business decisions; it does not require scaffolding or starting a server.

During implementation, keep the stable plugin ID and previously applied migration
SQL bytes. Append migrations for schema changes, including data backfills and
their failure handling. Complete the design when every requested operation maps
to an owning service, data changes, user feedback and a checkable outcome.
