# Database configuration

Database settings are resolved once at startup. Changing the provider or target
requires a restart. Opening a different target never copies, moves, synchronizes,
or deletes data from the previous target. Keep credentials in the environment,
not in committed application files. Toolframe does not automatically load `.env`.

`DatabaseSettings.from_env()` is the shared Python configuration boundary.
The launcher passes the original invocation directory and an explicit CLI path
to Python. `check`, `invoke`, and `serve` all resolve the same settings. Rust never
opens the database or runs migrations. Tools, plugins, MCP calls, Streamlit, and
the information page share the selected provider.

| Setting | Default | Meaning |
| --- | --- | --- |
| `TOOLFRAME_DB_PROVIDER` | `sqlite` | `sqlite`, `postgresql`, or importable `module:factory` |
| `TOOLFRAME_SQLITE_PATH` | see precedence below | SQLite file |
| `TOOLFRAME_DB` | see precedence below | Backward-compatible SQLite file variable |
| `TOOLFRAME_POSTGRES_DSN` | none | Required for PostgreSQL; `postgresql://` or `postgresql+psycopg://` URL |
| `TOOLFRAME_DB_TIMEOUT` | `5` | Positive connection/statement timeout in seconds, at most 300 |
| `TOOLFRAME_DB_POOL_SIZE` | `5` | Maximum pooled connections, 1–128 |
| `TOOLFRAME_DB_POOL_TIMEOUT` | `5` | Positive pool checkout timeout in seconds, at most 300 |
| `TOOLFRAME_DB_MAX_ROWS` | `10000` | Positive hard result row limit |
| `TOOLFRAME_DB_MAX_BYTES` | `10485760` | Positive hard result byte limit |

SQLite path precedence is explicit `--database`, then `TOOLFRAME_SQLITE_PATH`,
then `TOOLFRAME_DB`, then `<executable root>/data/data.db`. Explicit relative paths
resolve against the original invocation directory. The default resolves against
the executable root. Empty explicit values fail validation; they do not silently
fall back. Provider selection is independent of the SQLite path. Selecting
PostgreSQL does not use the SQLite file.

The settings object is immutable. Its public diagnostics omit the username,
password and URL query parameters. Invalid configuration fails with secret-free
errors. `identity` is an opaque target identity; it is not a connection URL.

Example environment (replace the placeholder in your shell; do not commit it):

```sh
export TOOLFRAME_DB_PROVIDER=postgresql
export TOOLFRAME_POSTGRES_DSN='postgresql://toolframe:REPLACE_ME@localhost:5432/toolframe'
export TOOLFRAME_DB_POOL_SIZE=5
```

For SQLite, leave the provider unset and optionally set
`TOOLFRAME_SQLITE_PATH=/absolute/path/data.db`. Configuration parsing itself does
not create files, open connections, or run migrations.

`check` trial-applies migrations to a verified filesystem copy of SQLite, including
its WAL sidecars; it never opens the original file. PostgreSQL check validates
history, archived artifacts, SQL policy and the dependency plan with read-only
connections. It does not trial-execute PostgreSQL DDL or start custom plugins.
The JSON report states `schema-trial` or `history-and-plan` respectively. Application
writes through the supported database/idempotency helpers are disabled during
preflight. Trusted application code must also keep imports free of side effects.

`invoke` and `serve` archive and apply migrations before dependent plugins become
available. Database calls at import time fail with `database_not_ready`; capture
the handle at import time, then use it inside tool calls, render functions or
plugin setup. A migration failure prevents readiness. Disabling a plugin affects
availability, while its installed schema and data remain in place.
