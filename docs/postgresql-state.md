# PostgreSQL Core state

Core can keep its management journal in PostgreSQL instead of `data/core.sqlite`.
The journal contains candidates, selected plugin versions, operation history,
idempotency keys and recovery metadata. It is independent of the optional
`sql-postgres` plugin, which owns business and authentication data.

Use one Core process per application, Python 3.14+, and PostgreSQL 16+. SQLite is
the default and keeps its existing schema history. PostgreSQL uses a **dedicated
Core database** with a restricted role; it may share the PostgreSQL server with
business databases. Do not provision Core tables in a business-provider database.

## Start a fresh application

Install the coordinated Core release with its optional driver:

```sh
pip install 'toolframe-core[postgres]==0.8.3'
toolframe-core --home ./app init
```

Read the stable `[application].id` from `app/toolframe.toml`. Supply a cluster
administrator DSN as `TOOLFRAME_CORE_SETUP_DSN` through your secret manager, then:

```sh
toolframe-core state setup \
  --database example_core \
  --application-id '<application id>' \
  --node-id '<stable host or node identity>' \
  --output ./app/.toolframe/core-environment.json
unset TOOLFRAME_CORE_SETUP_DSN

toolframe-core state run \
  --env-file ./app/.toolframe/core-environment.json -- \
  toolframe-core --home ./app serve
```

Setup creates a fresh database, role and schema. It refuses existing resources
and never overwrites the private output document. Runtime does not receive
cluster-admin privileges. Retain the generated environment document securely;
it is created with mode 0600. The helper exports its values and replaces itself
with the requested command. It does not store credentials in `toolframe.toml`.

The native launcher selects the PostgreSQL extra when the backend environment
variable is set or a `state` command is used. Offline launchers need compatible
Psycopg wheels available in their dependency cache or explicit wheelhouse.

## Runtime environment

| Variable | Required value |
| --- | --- |
| `TOOLFRAME_CORE_STATE_BACKEND` | `postgresql`; omitted or `sqlite` selects SQLite. |
| `TOOLFRAME_CORE_DATABASE_URL` | The dedicated Core database's restricted-role DSN. |
| `TOOLFRAME_CORE_APPLICATION_ID` | Must match the provisioned database and composition. |
| `TOOLFRAME_CORE_NODE_ID` | Stable identity of the host that owns the workers. |

Containers may inject these values directly. The journal connects before optional
plugins start; an invalid composition can be diagnosed using the last valid
composition in the journal. A mismatched database/application identity is refused.
Plugin workers and package-build subprocesses do not inherit `TOOLFRAME_CORE_*`
variables. This credential handling does not turn trusted Python plugins into a
sandbox.

Backend selection applies to a new application home. An existing `core.sqlite`
file is refused when selecting PostgreSQL: changing a DSN is not a data migration.
There is no automatic SQLite export/import or silent fallback to SQLite on error.

## Ownership, failures and restart

Core takes a PostgreSQL session advisory lock before claiming the journal, in
addition to the local home lock. A second Core using that database is refused,
including from a different application home. Normal shutdown clears the durable
ownership marker only after worker cleanup is proven.

A lost connection, cancelled database operation or uncertain commit invalidates
the journal session. Core stops accepting new operations, closes application
entrances and attempts graceful worker disposal. The database retains an ownership
marker so another Core cannot automatically take over merely because the database
lock was released. This is single-owner protection, not automatic failover.
An operation may have committed before its response was lost. After recovery,
inspect its ID or retry with the **same** idempotency key; do not blindly submit a
new operation. Startup marks unfinished operations interrupted and reconciles
runtime observations before activation.

If startup reports `core_state_recovery_required`, first stop the old Core and
verify that all its workers and descendants exited, on the recorded node. Then
use the runtime environment to inspect and acknowledge that cleanup:

```sh
toolframe-core state run --env-file ./app/.toolframe/core-environment.json -- \
  toolframe-core state inspect

toolframe-core state run --env-file ./app/.toolframe/core-environment.json -- \
  toolframe-core state recover \
    --expected-run '<owner_run from inspect>' --confirm-workers-stopped
```

Recovery refuses a live lock or a changed ownership marker. It clears stale worker
inventory only after that explicit operator assertion. It cannot verify cleanup
on another machine for you. Node identities prevent treating an old node's process
IDs as local processes. PostgreSQL schema versions are checked; unknown versions
are refused rather than modified implicitly.

## Backups and restoration

Back up the Core database separately from business databases. Stop Core cleanly,
then use PostgreSQL client tools matching the server's major version and private
output files to make a custom-format `pg_dump`. Supply credentials through libpq's
environment/secret mechanisms, not a password-bearing command argument.

For restoration, provision a **new** Core database with the original application
ID and a new private environment document. Restore the trusted Core archive into
that new, unused database with `pg_restore --clean --if-exists --no-owner --no-acl`,
connecting as its generated Core role. This replaces the empty setup schema and
leaves ownership with the new role. Never point that command at a live database.
Verify its application identity, history and ownership marker before starting a
single Core with the restored environment. A dump taken during an unclean run
still requires explicit recovery after verification of the old workers.

Also retain the matching application home: composition, lockfile, private keys,
management token and immutable artifacts are outside this journal. Restoring only
the database cannot reconstruct missing wheels or keys. A business-provider
backup does not contain the Core journal, and a Core backup does not contain
business data; retain a coordinated recovery point for both.

## Verification and limits

Run the shared SQLite/PostgreSQL journal contract and failure tests with:

```sh
uv sync --locked --group dev --extra postgres
# Supply an isolated server as TOOLFRAME_CORE_TEST_ADMIN_DSN.
uv run --no-sync python scripts/ci/check-postgres.py
```

These tests create and remove only their own random databases and roles. The
explicit check requires the driver and DSN; bare pytest skips PostgreSQL cases
when they are absent. The tests cover transaction rollback, concurrent idempotency,
history pagination, cancellation, lost commit acknowledgement, connection loss,
ownership/recovery, and the public management interface.

The Core is still a single-host worker supervisor with a persistent application
home. PostgreSQL state does not enable concurrent Core replicas, automatic node
failover, untrusted plugins, or diskless recovery. Optional `validate --runtime`
scratch mode is refused when the Core journal is external; use a separately
provisioned application and databases for runtime validation. Ordinary preflight
validation remains available.
