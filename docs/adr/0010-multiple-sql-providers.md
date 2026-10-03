# ADR 0010: Bind managed stores to retained SQL providers

Status: accepted.

## Context

Applications may keep some plugin data in local SQLite while using PostgreSQL for
other owners. A singleton storage-provider kind unnecessarily prevented that
composition, even though service requirements already had explicit bindings.
Uncontrolled reassignment could instead make retained data appear lost by opening
an empty store in another backend.

## Decision

Allow multiple storage providers and reuse service defaults and per-consumer alias
bindings. Keep one SQL provider per plugin. Persist each initialized store's
provider in the Core journal and reject binding changes for retained stores.
Disable and removal preserve the assignment. Adopt unambiguous prior ownership
when upgrading a journal that predates these records.

Use the prospective graph for management operations and snapshot bindings in
admitted roots. Bind migration tokens to the provider and its generation. Record
the provider with uncertain schema outcomes and prevent unrelated replacement
from clearing forward-repair evidence. Attempt cleanup on every leased provider
even if an earlier cleanup fails.

## Consequences

Business plugins keep the existing SDK client and SQL service contract. Different
owners can choose different backends without a global storage switch. Shared-route
presentation providers retain their singleton rule. The journal remains
independent of plugin storage.

Retained stores cannot switch backend using a configuration edit. Cross-provider
copying and distributed transactions need separate designs and are unsupported.
Provider identity is a plugin ID, not proof that an operator kept the same remote
database endpoint. Ambiguous legacy ownership is conservative and requires repair.

See [SQL storage configuration](../sql-storage.md) for operator instructions.
