# Replaceable Core journal with explicit PostgreSQL ownership

Status: accepted

## Decision

Core uses an asynchronous `CoreState` interface with SQLite and PostgreSQL
implementations inside the Core distribution. PostgreSQL is an optional dependency
selected by runtime environment, initialized before the plugin lifecycle. The
SQLite implementation retains its existing schema and runs its connection on one
dedicated executor thread; PostgreSQL uses an async connection. Both serialize
each journal operation and expose the same management behavior.

PostgreSQL Core state uses a dedicated database and role. Plugin storage remains
an independently managed service and cannot be a bootstrap dependency of Core.
Core/business backups remain separate, avoiding accidental rollback of management
history during a plugin-store restore. No implicit SQLite migration is performed.

A session advisory lock and durable owner marker restrict the database to one
Core. Clean shutdown releases ownership only after verified worker disposal.
Unknown database outcomes invalidate the session and gate application entrances.
Unclean ownership requires explicit operator recovery after old-worker cleanup;
releasing a database connection alone is insufficient evidence for takeover.

## Reasons and tradeoffs

Journal access must not block the host event loop on network I/O. Keeping dialects
behind the interface confines SQL/schema differences without making the Core
depend on a plugin that it must first activate or repair. Caching known redaction
values lets error reporting sanitize output even when the database is unavailable.

An explicit recovery step sacrifices automatic failover to preserve the existing
single-supervisor lifecycle contract. Node identity avoids confusing historical
remote PIDs with local processes, but does not create a distributed worker
supervisor. Configuration, credentials and artifacts remain outside the journal;
their externalization and multi-Core execution require separate designs.
