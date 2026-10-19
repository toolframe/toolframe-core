# Multiple SQL storage providers

One application can run SQLite and PostgreSQL storage providers together. Each
plugin owns one managed store and continues to use the SDK's `context.sql()`
client. The Core journal is configured separately; see
[PostgreSQL Core state](postgresql-state.md).

## Configure a fresh composition

Install both providers, then choose bindings before enabling store-owning plugins.
For example, with `sql-sqlite` and `sql-postgres` enabled:

```toml
[bindings]
"toolframe.sql" = "sql-postgres"
"entry-register/sql" = "sql-sqlite"
```

The service binding selects PostgreSQL by default. The `plugin-id/alias` binding
overrides it for Entry Register's `sql` requirement. Auth and other consumers
without overrides use PostgreSQL. Provision every PostgreSQL owner and inject its
runtime credentials before starting the host, following that provider's setup
guide. SQLite needs no external server. Both providers use SQL service version 1.

With exactly one matching provider, a requirement can resolve without an explicit
binding. With several matches and no applicable binding, composition fails with
`service_ambiguous`. A binding to a disabled or absent provider leaves its required
consumers blocked. It never redirects their calls to another database.

After editing `toolframe.toml`, review and apply the composition:

```sh
toolframe --home ./app plan
toolframe --home ./app apply --plan-id PLAN_ID
toolframe --home ./app status
```

The plan's `stores` field shows proposed and retained providers. Status shows the
active resolution and retained ownership. Dependency diagnostics show each
consumer's resolved storage edge and the affected cohort for reload or disable.

## Add PostgreSQL to an existing SQLite application

Keep the SQLite service default and override only new PostgreSQL store owners:

```toml
[bindings]
"toolframe.sql" = "sql-sqlite"
"orders/sql" = "sql-postgres"
```

Here `orders` is an example new plugin ID and `sql` is its SQL requirement
alias. Use the names declared by the plugin you are installing.

1. Back up the application home and its existing stores. Add the bindings while
   the original SQLite provider remains enabled.
2. Provision the new PostgreSQL owner, supply its runtime environment, and install
   and enable the PostgreSQL provider before enabling that owner.
3. Review `plan` and confirm that existing owners retain `sql-sqlite`. Apply
   the reviewed composition, then verify the new plugin through its public service.

The existing SQLite data stays in its original stores. If PostgreSQL should later
become the service default, first add a `plugin-id/alias` override to SQLite for
every SQL alias of each retained SQLite owner. Review those overrides together
with the default change. Changing the default alone would request reassignment
of existing stores and is rejected.

## Retained stores and migrations

Core records a store's provider before initialization or migration. Disable,
removal, reinstall and host restart retain that association. Changing the binding
of a retained store fails with `store_rebind_required`; restore its original
binding to recover. This includes changes made while Core is stopped. On upgrade,
Core adopts the previous journal's provider selection; ambiguous historical
ownership requires repair instead of an automatic reassignment.

Bindings do not copy data between SQLite and PostgreSQL. Cross-provider data
migration is not supported. A provider's plugin ID is its routing identity;
operators must also preserve the database and credentials behind that identity.
Changing an endpoint is not a data migration.

Schema upgrades stay within the owning provider:

```sh
toolframe --home ./app migrate plan entry-register
toolframe --home ./app migrate apply entry-register --plan-id PLAN_ID
```

The reviewed token includes provider identity and worker generation, as well as
the candidate, composition and schema checks. Reloading a provider invalidates
its outstanding migration plans. A committed or uncertain upgrade records the
owner and provider in `forward_repair`; recover that store before unrelated
replacement operations. See the [architecture contract](architecture.md#data-ownership).

## Plugin and lifecycle limits

A managed store must declare a required `toolframe.sql` dependency. All
SQL aliases in one plugin must resolve to the same provider. SQL consumers must
target a storage provider. Plugins needing another owner's data call its service.

Reload or disable drains the selected provider and its required dependents. Other
cohorts retain their workers unless they share required service dependencies or
presentation resources. An admitted call retains its bindings for all nested
calls, and cleanup attempts every storage provider leased by that call.

Transactions are local to one owner's store. There is no cross-store atomic
commit. PostgreSQL migrations need explicit PostgreSQL dialect SQL; arbitrary
SQLite SQL is not translated. Presentation providers that own shared routes remain
singletons. This feature does not add multi-Core or replica coordination.
