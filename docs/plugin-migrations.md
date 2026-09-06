# Plugin schema migrations

Schema identity is independent from service identity. A service dependency such
as `requires=(DATABASE,)` orders plugin services; it does not claim tables or grant
permission to another owner's schema. A migration dependency such as
`entry_register@2` identifies an exact schema revision. Cross-plugin business
operations should normally call the owning plugin's public service methods.

The coordinator in `toolframe_migrations` runs during the host database plugin's
setup, before dependent plugins, MCP calls and Streamlit become available. `check`
uses the same planner; its provider-specific validation limits are documented in
[database configuration](database-configuration.md).

## Scaffold a domain revision

```sh
toolframe scaffold migration inventory create_items
```

This requires an existing `inventory` domain. It appends an owner-local revision
to `domains/inventory/migrations/schema.json` and creates separate SQLite and
PostgreSQL SQL drafts. Complete both SQL files and the revision's `tables` list,
then run `toolframe check`. An unfinished draft intentionally fails validation.
The previous schema target becomes an explicit dependency; extending an existing
legacy domain depends on its last preserved legacy revision. Existing SQL bytes
and legacy numbering never change. Only Python chooses revision identifiers.

Scaffolding uses an exclusive `.scaffold.lock` and atomically replaces the JSON
manifest. Concurrent scaffolds fail without overwriting another writer. After a
process crash, inspect draft files and the manifest before removing a stale lock.
This authoring command does not connect to either database.

## Declare immutable revisions

```python
from pathlib import Path
from toolframe_migrations import Revision, Schema

HERE = Path(__file__).parent
SCHEMA = Schema(
    owner="inventory",
    target="2",
    revisions=(
        Revision(
            "1",
            sql={"sqlite": HERE / "sqlite/1.sql", "postgresql": HERE / "postgresql/1.sql"},
            tables=("inventory_items",),
        ),
        Revision(
            "2",
            sql={"sqlite": HERE / "sqlite/2.sql", "postgresql": HERE / "postgresql/2.sql"},
            tables=("inventory_items",),
            depends=("inventory@1",),
        ),
    ),
)
```

`owner` is a stable business identifier. Every owned table and index uses its
`owner_` prefix. Each revision declares the tables it creates or mutates; these
declarations are immutable after application. Add a new revision when adding a
table. Views follow the same ownership rule. Ownership remains reserved after
the plugin is removed, even when a later migration removes the physical table.

`depends` gives explicit revision references. Revision names are identifiers,
not an implicit ordering rule: revision `2` must explicitly depend on `1`.
Dependencies can cross owners. A migration that reads another owner's table
also declares `reads=("other_owner_table",)` and an exact revision dependency on
that owner. A read declaration never permits mutating that owner's table.

A domain may instead place `schema.json` in its `migrations` directory:

```json
{
  "owner": "inventory",
  "target": "1",
  "revisions": [
    {
      "revision": "1",
      "tables": ["inventory_items"],
      "depends": [],
      "sql": {"sqlite": "sqlite/1.sql", "postgresql": "postgresql/1.sql"}
    }
  ]
}
```

JSON paths stay inside the manifest's package. Python declarations may reference
SQL bundled with an installed package. Source files must be regular UTF-8 files
up to 4 MiB each. SQL variants are explicit; changing providers does not translate
schema SQL or transfer any data. A missing selected-provider variant fails before
application.

## Durable history and failure recovery

The coordinator first checks dependencies, checksums, target compatibility and
ownership under a database lock. It durably commits complete migration artifacts
and a `prepared` run **before** executing plugin SQL. Each artifact contains the
original SQL bytes, source filenames, per-provider hashes, owner, tables, read
contracts, dependencies and format version. The archive lives in the selected
database; it does not depend on a runtime cache, source checkout or installed
Python module.

The coordinator reacquires the lock and re-plans before applying SQL, because
another process may have completed the same work. Pending SQL, applied revision
rows and the successful run status commit in one transaction. On failure they
roll back together; archived artifacts remain, and a separate update records the
failed run. A process interruption can leave a `prepared` run. On restart the
applied ledger determines committed work; remaining work is retried from verified
artifacts and installed declarations. Failure-record updates can themselves fail
when the database is unavailable; they do not replace the original error.

SQLite uses an immediate writer lock. PostgreSQL uses a transaction-scoped
advisory lock. Runtime read limits do not truncate internal artifacts. All
installed sources are rechecked between archival and execution.

The supported SQL subset covers persistent tables, indexes and views, ordinary
data mutations, and a bounded set of pure SQL functions. Transaction commands,
database administration, procedural functions/triggers, concurrent indexes,
temporary/unlogged objects and cascading DDL are rejected. These bounds permit
transactional schema application on both providers. PostgreSQL sequence counters
can still have gaps after rolled-back inserts. External side effects from
manually installed database objects are outside this guarantee; plugins and
database administrators are trusted, not sandboxed.

## Removal and reinstallation

Disabling a service, closing a provider or uninstalling a plugin does not drop
tables, remove rows, delete receipts or erase migration history. The coordinator
verifies applied history from the durable archive when source packages are absent.
It never selects an absent plugin's unapplied migration merely because an artifact
was archived during an earlier failed attempt. An absent, unapplied dependency
does not satisfy an installed plugin's requirements.

Reinstallation verifies immutable applied revisions and the code's target.
Older/divergent targets fail instead of silently downgrading. A schema declaration
may explicitly list compatible later heads, for example `compatible_heads=("3",)`.
This records the author's compatibility promise and performs no downgrade. A
dependency also cannot force another installed plugin beyond its declared target
or compatible heads. There is no purge operation.

## Preserve the legacy global history

Unclaimed direct `.sql` files in domain migration directories remain the explicit
legacy global sequence `legacy@0000`, `legacy@0001`, and so on. Files claimed by a
namespaced manifest are excluded from that global scan. Legacy filenames,
numbering, UTF-8 bytes and checksums remain immutable. Original applied rows in
`_toolframe_migrations`, including timestamps, are verified and retained during
adoption; they are not renumbered or rewritten.

The existing `0000_bootstrap.sql` and `0001_adopt_domain_table_name.sql` are unchanged.
Their PostgreSQL companions are under `migrations/postgresql/`. The preserved
global ledger pins the original canonical SQLite source hashes; durable artifacts
add the exact PostgreSQL companion bytes and hashes actually used on that provider.
The historical `dashboard_example_entries` name is an explicit ownership exception
belonging to Entry Register until its preserved rename migration runs.

Before first adoption, original legacy source is required to verify an existing
ledger. After archival, removing its plugin leaves the history verifiable. New
Entry Register revisions should declare a dependency on `legacy@0001` when they
need the existing table. No applied legacy source file should be edited to add
features or support another provider.
