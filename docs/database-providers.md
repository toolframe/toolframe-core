# Database service and providers

Business code consumes `DatabaseService`; provider plugins implement it. The
stable typed reference is `toolframe_database_service.DATABASE`, whose service
name is `toolframe.database`. A plugin's concrete name may change without
changing consumers. The generic `fastapi-cordis` package does not depend on a
database driver or Toolframe.

The SQLite and PostgreSQL providers implement the same operations. Perform this
work inside a tool, render function or plugin setup after schema readiness:

```python
from toolframe_db import get_database

database = get_database()
identifier = database.insert("entry_register_entries", {"label": "Example", "value": 2.5})
with database.transaction() as transaction:
    transaction.execute(
        "UPDATE entry_register_entries SET value=:value WHERE id=:id",
        {"value": 3.0, "id": identifier},
    )
```

- `query(sql, params, max_rows=None, max_bytes=None)` returns a bounded pandas
  DataFrame and accepts read-only queries. Per-call limits cannot raise the
  configured hard limits. Both raw row sizes and DataFrame memory are checked.
- `execute(sql, params)` commits one data mutation and returns `ExecuteResult`
  with `affected_rows` and optional `rows` from `RETURNING`.
- `insert(table, values, id_column="id")` returns an integer identifier using
  `RETURNING`. It validates identifiers and binds every value. IDs can have gaps;
  a failed PostgreSQL transaction does not rewind its identity sequence.
- `transaction()` owns one connection until commit or rollback. An operation
  failure poisons the transaction even when application code catches that error.
  Nested transactions and operations on a finished transaction are rejected.
- `run_once(namespace, key, input, callback)` stores the business mutation and a
  checksummed JSON receipt in one transaction. Retries with the same input replay
  the stored result. Different input, corrupt receipts, or oversized results fail.
  The callback receives bounded query/execute/insert operations on that same
  transaction. These guarantees cover database writes, not external side effects.
- `close()` closes resources only. Data, tables, migration history and idempotency
  receipts remain. Host plugin leases must drain before cleanup.

Use named `:parameter` bindings for portable business SQL. Positional `?` bindings
remain supported for simple legacy queries; use named bindings when PostgreSQL
JSON operators also contain `?`. Binding conversion preserves string literals,
comments, dollar-quoted strings and PostgreSQL casts. SQL syntax itself must still
be supported by the selected provider. Provider-specific schema SQL belongs in
migrations, not business callers.

`last_insert_rowid` and `lastrowid` are legacy SQLite result aliases. PostgreSQL
does not invent these values: use `insert()` or explicit `RETURNING` instead.
The Entry Register model demonstrates one unchanged consumer for both providers,
including optimistic version checks and application-generated UTC timestamps.

The startup-selected service rejects schema changes and access to runtime-owned
tables. Idempotency callbacks use the same policy and cannot perform maintenance
or inspect their private receipt table. This intentionally tightens the old
SQLite-only callback API, which previously allowed business-table `REINDEX`.
The historical standalone `DashboardDatabase(path)` alias remains an explicit
SQLite development helper with DDL enabled; it is not the configured host service.

SQLite owns native authorization, WAL setup, fresh operation connections, busy
timeouts, statement progress deadlines, and immediate writer locking. PostgreSQL
owns SQLAlchemy pooling, psycopg connections, server query cursors, statement and
lock timeouts, read-only query transactions, and transaction-scoped advisory
locks for idempotency. Pool exhaustion and lock/statement timeouts are retryable
structured failures. Driver errors never place PostgreSQL credentials in public
errors or logs.

The SQL guards protect trusted application code from accidental misuse. They
are not a sandbox for hostile Python, database functions, or database superusers.
Do not give untrusted plugins in-process execution access.

Provider construction is lazy. `create_provider(settings)` accepts built-ins or
a `module:factory` imported from installed application dependencies. A factory
receives `settings`, `logger`, and `audit_sink` and returns `DatabaseProvider`.
Its host integration also exposes `dialect`, `identity`, `connection()` and
`begin_locked()`. These raw operations belong to migration coordination, not
MCP tools or browser endpoints. A third-party provider can extend a supported
SQLite/PostgreSQL implementation without modifying Rust. New SQL dialects need
an explicit migration contract extension rather than pretending to be either.

Configuration and restart behavior are documented in
[database-configuration.md](database-configuration.md). The low-level database
service is shared by trusted business code; it is not a per-plugin SQL sandbox.
Schema ownership and cross-owner read declarations are enforced by the migration
coordinator. Prefer a plugin's public business service for cross-plugin operations.
See the [owned business-service example](../examples/plugins/README.md).
