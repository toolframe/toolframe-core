# Database migrations

This directory is the source of truth for the `data/data.db` schema. The Rust
launcher validates and applies these files before it starts Python or opens the
HTTP listener.

## Add a schema change

1. Never edit, rename, or delete a migration that has run against a database.
2. Copy the next four-digit sequence into a new direct child such as
   `0001_add_status.sql`.
3. Put schema or data SQL in that file. The launcher supplies one transaction,
   so do not add transaction statements or connection-level configuration.
4. Update the dashboard widget or MCP tool that uses the changed schema in the
   same code change.
5. Restart `lite-mcp`. Migrations run only at process startup.

Example:

```sql
-- 0001_add_status.sql
ALTER TABLE dashboard_example_entries
ADD COLUMN status TEXT NOT NULL DEFAULT 'open';

CREATE INDEX dashboard_example_entries_status_idx
ON dashboard_example_entries(status);
```

Migration names must be contiguous `NNNN_description.sql` files starting with
the exact `0000_bootstrap.sql`. Descriptions may use lowercase ASCII letters,
digits, `_`, and `-`. Files must be direct, regular, non-symlink UTF-8 files no
larger than 4 MiB.

All pending migrations run atomically in numeric order. The launcher records
each filename and its exact-byte SHA-256 checksum in
`_lite_mcp_migrations`. Editing applied history therefore fails startup instead
of silently changing an existing database.

Migration SQL may contain multiple statements, but must not contain `BEGIN`,
`COMMIT`, `ROLLBACK`, `SAVEPOINT`, `RELEASE`, `PRAGMA`, `ATTACH`, or `DETACH`.
`CREATE TRIGGER` is also prohibited because a deferred trigger body could
bypass internal table protection. The launcher owns transactions and SQLite
configuration. Back up `data/data.db` before a destructive schema or data
migration.

`tools/execute_sql.py` is a bounded fallback for app-data reads and DML. Its
runtime adapter rejects DDL and schema administration. Durable schema changes
belong here and must be authored by the primary coding agent, checked, and
applied on restart.

## Runtime-owned internal schema

The launcher creates and validates `_lite_mcp_migrations` and
`_lite_mcp_idempotency` directly at every startup. They are not user
migrations and do not consume migration numbers. User migration SQL cannot
read or modify either table. Do not create, alter, drop, or repair them in a
migration; an incompatible internal schema fails startup with a recovery
diagnostic.
