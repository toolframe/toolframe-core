# Plugins and durable data

Read this for Develop work, including business changes without a migration, and
for service recovery, schema/provider changes or target selection. Finish when
the service contract, connected dependency graph, schema plan, MCP/UI calls and
preservation tests agree with the installed executable.

## Service boundary

Use a complete business capability as the default plugin development unit. Keep
its ordinary rules, entities and persistence in the owning domain's model, behind
one meaningful service interface. Extend that plugin for small changes. An external
client/resource may be its own supporting plugin when it has an independent lifetime.
Local business code can stay in the app; packaging is needed only for actual reuse
or distribution. Changed models, contracts, catalogs or packages require restart.

| Identity | Stockroom example | Responsibility |
| --- | --- | --- |
| Business owner | `stockroom` domain/schema owner | Model, tables, migrations, tools, UI and peer guide |
| Public ServiceKey | `STOCKROOM`, named `stockroom.service` | Typed business interface consumed through requires/leases |
| Concrete Plugin ID | `stockroom.local` | Creates the service, validates settings and owns any resources |
| Host domain gate | `domain.stockroom` | Automatically guards discovered tools/widgets |

Define the business plugin in `domains/stockroom/model/plugin.py`. The composition
catalog imports that definition and connects the existing gate:

```python
from domains.stockroom.model.plugin import PLUGIN

PLUGINS = [PLUGIN]
DOMAIN_PLUGINS = {"stockroom": (PLUGIN.id,)}
```

Its local `migrations/schema.json` is discovered once by the host. The plugin
requires `DATABASE`; use `SCHEMAS` instead for a packaged schema without that local
manifest. Add genuine extra dependencies to `app/pyproject.toml`, restart to install
them, and register imported definitions in the catalog. Runtime registration does
not perform installation. The [copyable Stockroom app](../assets/business-plugin/README.md)
has no extra dependency and exercises the fastapi-cordis wheel supplied by Toolframe.

For interchangeable implementations, define a runtime-checkable Protocol and a
`fastapi_cordis.ServiceKey`. Providers declare `provides=KEY`; consumers declare
`requires=(KEY,)`, obtain it through `ctx.require(KEY)` during setup, and use
`get_plugins().lease(KEY)` or `use_plugin(KEY)` around later work. Concrete plugin
names can change while the service contract stays stable. Register cleanup as
soon as a resource opens. A plugin's background task must stop and join during
cleanup. Hold leases until the actual work finishes.

MCP uses `with use_plugin(KEY)` and widgets use `with context.plugins.lease(KEY)`
around the same business service. Across domains, import the key/Protocol only
from the provider's root `__init__.py`, declare it in a retained consumer's
`requires`, then obtain it with `ctx.require`. A public convenience function must
own its call-time lease. Importing model functions directly bypasses this boundary.
Toolframe prepares the domain namespace before catalog loading and retains model,
contract and service class identities; widget reruns reload UI modules only.

The embedded fastapi-cordis 0.2.0 host validates a complete enabled startup graph.
During runtime, ordinary Stop blocks consumers while retaining their enabled
intent; restoring dependencies resumes them. Cascade Stop also disables their
intent. A failed plugin needs explicit Retry after correcting its cause; Enable
or a config update does not clear failed state. Use `/info` or `/api/plugins` to
inspect `blockedBy`, generation/config revision and task/lease diagnostics.

Preview complete replacement settings with `POST /api/plugins/<id>/config/plan`.
Inspect `affected`, `protected` and `canApply`; applying with `PUT .../config`
revalidates the current graph. Equivalent normalized config does not restart
services. Do not request, store or expose secret config through snapshots/plans.
Host database/artifact services stay protected.

Own asynchronous work with `ctx.task` and cleanup with a child `ctx.scope()` /
`EffectScope`. Tasks must cooperate with cancellation. `EventKey` handlers belong
to the plugin lifetime; events are local, non-durable and not tenant-isolated.
They do not replace audit records or an outbox. Runtime `register`/`unregister`
only changes already available service definitions: it does not install packages,
add MCP/widget catalogs or discover/apply migrations. Those require host startup.

`DATABASE` from `toolframe_database_service` is the stable database key. The
selected host plugin is `toolframe.database.sqlite` or
`toolframe.database.postgresql`. Host services cannot be disabled from management
routes. Domain tools and widgets share `domain.<name>` availability. Stopping a
plugin drains calls and closes resources; it does not delete schema or data.

## One selected database

The default remains SQLite at `<executable root>/data/data.db`. Selection is read
once by Python for `check`, `invoke` and `serve`:

- `TOOLFRAME_DB_PROVIDER`: `sqlite`, `postgresql`, or an installed `module:factory`.
- SQLite path: explicit `--database`, then `TOOLFRAME_SQLITE_PATH`, then the
  compatible `TOOLFRAME_DB` variable, then the default. Relative explicit paths
  use the original invocation directory.
- PostgreSQL: `TOOLFRAME_POSTGRES_DSN`. Put credentials in the environment;
  Toolframe does not load `.env` automatically. `/info` reports a safe target.
- Bounds and pool settings: `TOOLFRAME_DB_MAX_ROWS`, `TOOLFRAME_DB_MAX_BYTES`,
  `TOOLFRAME_DB_TIMEOUT`, `TOOLFRAME_DB_POOL_SIZE`, `TOOLFRAME_DB_POOL_TIMEOUT`.

Changing provider or target requires restart. It never copies, moves or deletes
old rows. Returning to the previous target recovers its own data and receipts.
If data must move, design and authorize a separate migration/transfer task.

Business service setup injects `DATABASE`; its implementation owns database work.
The compatible `toolframe_db.get_database()` helper remains available for existing
trusted runtime code. Capturing the handle at import time is allowed; executing queries at import
time is rejected before schema readiness. Named `:parameter` bindings and
`insert(table, values)` provide portable inputs and returned integer IDs.
PostgreSQL does not provide SQLite's `lastrowid`; IDs can have gaps.

`transaction()` rolls back on failure, including a caught database operation
failure. `run_once(namespace, key, input, callback)` commits business writes and a
checksummed receipt together. Retry the same intent with the same key and input.
External requests and files are outside that database transaction. Callbacks use
bounded query/execute/insert methods; private `_toolframe_*` tables are inaccessible.

## Schema authoring

Run `toolframe scaffold migration <domain> <description>`. Complete the generated
SQLite and PostgreSQL SQL drafts and the new revision's owned `tables` in
`domains/<domain>/migrations/schema.json`. The manifest's `owner` matches the domain;
`target` names the revision its code needs. Each revision declares:

```json
{
  "revision": "0001_create_items",
  "sql": {"sqlite": "sqlite/0001_create_items.sql", "postgresql": "postgresql/0001_create_items.sql"},
  "tables": ["inventory_items"],
  "depends": [],
  "reads": []
}
```

New numbers are local to the owner. Keep applied SQL and revision metadata
immutable; append a revision that depends on the previous `owner@revision`.
Preserve the original global `0000`/`0001` files and their ledger. A new Entry
Register revision that needs the old table depends on `legacy@0001`.

Package plugins may export a `toolframe_migrations.Schema` with `Revision`
objects or load one using `read_schema(Path(...))`. Register it in
`app/plugins.py` as `SCHEMAS = {PLUGIN.id: SCHEMA}` beside `PLUGINS = [PLUGIN]`.
Toolframe adds the database dependency and prepares installed schema targets
before dependent setup. A disabled but installed plugin still declares its schema.

Prefix tables and indexes with their stable owner. A table has one retained owner,
including after uninstall. Runtime `requires` grants no schema access. Prefer
public business service methods across plugins; a migration that reads another
owner must list that table in `reads` and depend on its exact owner revision.
Only owned tables may be mutated. The shared low-level database service is trusted
application code, not per-plugin SQL authorization or a hostile-code sandbox.
Put each mutation in a separate top-level statement: nested mutations,
data-modifying CTEs, SELECT INTO and explicit row locks are unsupported. A read
declaration never authorizes an indirect write.

Migrations support bounded transactional table/index/view DDL and data SQL.
Transaction control, PRAGMAs, attached databases, triggers, procedures, concurrent
DDL and cascading operations are outside this contract. Python coordinates locks,
archives exact source artifacts before execution, and records applied revisions.
Failed SQL rolls back its pending batch; committed artifacts and failed run history
remain. A crash may leave a prepared run; the next start replans from durable
applied history. PostgreSQL identity sequence gaps remain possible.

Uninstalling a plugin means removing its definition and schema declaration, then
restarting. Tables, data, ownership and verifiable archived history remain.
Partial legacy removal is validated against installed sources plus verified
applied archives. Missing fresh history or unapplied archives cannot fill gaps.
Archived pending SQL from an absent plugin does not execute. Reinstall checks
immutable checksums and target compatibility; older code requires an explicit
`compatible_heads` declaration before it may run against a newer schema.

## Verification

Run `check` first. SQLite uses a temporary filesystem copy, including WAL sidecars,
for a migration trial; PostgreSQL reads history and validates artifacts/SQL policy
and the plan without trial-executing DDL. The JSON report identifies the validation
level. Custom plugin setup is not run by check. Supported database/idempotency
helpers reject application writes during preflight; trusted imports must also
avoid independent external side effects.

Test the selected provider with disposable data. Verify real startup, business
CRUD, transaction rollback, repeated idempotent calls, shutdown/restart, and any
changed plugin lifecycle or schema compatibility. For target changes, prove the
old data remains and does not appear automatically in the new target. Record the
actual provider and validation level; a read-only check is not a PostgreSQL DDL run.

A third-party `DatabaseProvider` implements the public `normalize_error(error,
operation=...)` hook along with connection/locking operations. Preserve safe
error codes and retryability without logging raw credentials or exception text;
the migration coordinator does not rely on private helpers or a provider logger.
