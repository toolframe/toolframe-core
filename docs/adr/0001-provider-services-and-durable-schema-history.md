# Provider services and durable schema history

Status: accepted for implementation on `feature/revamp-dependency-plugin`.

Business callers depend on a stable, typed database service. A selected provider
owns connections, dialect-specific operations, transaction locking, and cleanup.
SQLite remains the default; PostgreSQL must pass the same observable contract.
The plugin runtime resolves service identity separately from provider identity,
so leases and dependency ordering follow the selected implementation.

Database configuration is resolved once at process startup. Switching a provider
or target means opening that target and preparing its required schema. It never
copies rows, imports another database, removes the previous target, or drops
plugin data. Ordinary lifecycle cleanup closes resources only.

Migration coordination lives in Python alongside the database Interface. The Rust
launcher prepares the runtime and passes configuration; it does not maintain a
second implementation of migration or database rules. Migration requirements run
before dependent plugin setup and readiness, independently from service cleanup.

New plugin revisions have an owner namespace and explicit revision dependencies.
Service dependencies do not grant table access. Schema ownership follows the
business identity and remains reserved after a plugin is removed. A schema owner
may publish a service or an explicit read contract for other plugins.

The selected database durably stores migration artifacts, metadata, dependency
references, and checksums before executing the scripts. Applied history remains
verifiable without the original package or runtime cache. Only installed plugins'
required revisions are selected for execution; archival alone never selects an
uninstalled plugin's pending work. Reinstallation verifies schema compatibility
and never silently downgrades a newer schema.

Existing globally numbered migrations remain an explicit legacy history. Their
filenames, source bytes, checksums, order, and applied records are preserved. The
new coordinator verifies and adopts that history rather than rewriting source or
renumbering revisions. SQL for a different provider is supplied explicitly; a
provider change is not a data conversion operation.

Only operations with the provider's supported transactional guarantees are
accepted as migrations. Unsupported nontransactional operations fail before
execution. A persisted run record distinguishes an applied transaction, a failed
attempt, and recovery after interruption; documentation must describe actual
guarantees rather than promise universal rollback.

## Implementation references

SQLAlchemy supplies portable parameter binding, result handling, and connection
management behind the database Interface. Provider hooks retain actual database
locking and read restrictions. See the official [engine documentation](https://docs.sqlalchemy.org/en/20/core/engines.html),
[SQLite transaction behavior](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html),
[Psycopg transactions](https://www.psycopg.org/psycopg3/docs/basic/transactions.html),
and [PostgreSQL locking](https://www.postgresql.org/docs/17/explicit-locking.html).
