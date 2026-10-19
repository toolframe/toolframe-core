# Offline application maintenance

`toolframe_core.maintenance.stopped_application(home)` is the public boundary for
an operator CLI that snapshots a stopped Toolframe application. It holds the same
kernel `HostLock` used by Core for the entire context, validates local persisted
state, and yields a versioned inventory. It does not start workers, repair state,
checkpoint SQLite, or run database migrations.

```python
from pathlib import Path
from toolframe_core.maintenance import stopped_application

with stopped_application(Path("my-app")) as inventory:
    manifest_metadata = inventory.as_dict()
    # Complete all file reads and snapshot work inside this context.
    for item in inventory.files:
        source_path = inventory.home / item.path
        # The operator's snapshot implementation copies/checksums source_path.
```

Stop Core cleanly before entering the context. A home must have completed a Core
startup and shutdown; creating `toolframe.toml` alone is insufficient. Application
files must remain under operator control throughout the snapshot. Other managed
Core processes cannot acquire the home lock until the context exits, including
when the snapshot raises an exception. Acquiring the real lock intentionally
updates the informational PID text in `.toolframe/host.lock`; database and
configuration contents are unchanged. Public lock-only
`application_lock(home: str | Path)` yields `None` and holds that same HostLock
without checking workers, backend settings, or databases. It requires an existing
regular home and refuses unsafe home/lock paths. It creates only the lock
directory/PID file. Use it for a new restore staging home; it is not a substitute
for source validation with `stopped_application`.

## Inventory format 1

The frozen `StoppedApplicationInventory` has `home: pathlib.Path` for local file
access. `as_dict()` omits this absolute path and returns the following JSON-safe
shape. All listed paths are relative POSIX paths beneath the source home. Files
and keys are ordered by path; callers should use the explicit identity fields,
rather than rely on ordering of stores or environment locks.

| Field | Shape and meaning |
| --- | --- |
| `format_version` | Integer `1`; consumers must refuse unsupported versions. |
| `status` | String `"STOPPED"`, established while holding the lock. |
| `application_id` | Persisted application identity, checked against the initialized journal and business stores. |
| `application_name` | The composition's display name. |
| `composition_revision` | SHA256 of the current `toolframe.toml`, matching the lock and the journal's last applied desired revision. |
| `files` | Array of `{path, role, size, mode, sha256}` for regular files to snapshot. `size` is bytes; `mode` is an integer permission bitmask. `sha256` is the verified digest for required immutable artifacts, otherwise `null`. |
| `stores` | Array of `{path, owner, provider}`. Core uses `owner: null, provider: "core"`; supported business stores identify their plugin owner and `"sql-sqlite"` provider. |
| `retained_keys` | Array of `{path, owner, name}` identifying credential files to retain. `owner` is `null` for the management token and undeclared retained key files. No key contents or key hashes are returned. |
| `dependency_wheels` | Array of `{environment_id, distribution, version, filename, sha256, path}` for recorded dependency wheels. A local `path` is verified against its locked hash; `path: null` is an explicit dependency rehydration prerequisite. |
| `environment_locks` | Array of `{candidate_id, purpose, environment_id, sha256, python_version, python_implementation, platform_system, platform_machine, requires_python}`. `purpose` is `"backend"` or `"ui"`; the digest identifies the recorded lock text. Interpreter metadata comes from the source environment's regular `pyvenv.cfg`. `python_version` preserves its known precision: `"3.14"` means the source patch version is unknown, while `"3.14.7"` records a full version. Platform fields describe the local source inspection host. |

The corresponding public record classes are `InventoryFile`, `SQLiteStore`,
`RetainedKey`, `DependencyWheel`, and `EnvironmentLock`. Inventory attributes use
tuples of those records; `as_dict()` converts them to arrays of objects.

File roles are `composition`, `lock`, `artifact`, `core_store`, `business_store`,
`retained_key`, and `home_file`. `home_file` preserves other regular in-home files,
including local attachments, plugin sources, and migration backups. Current,
previous, and last retained candidates are checked, including plugin rows removed
from the current composition. Backend and UI environment locks are both checked.
Captured plugin wheels and locally referenced dependency wheels must exist and
match their recorded SHA256 digests.

Direct references recorded as `packages.archive` are accepted only for compatible
wheel files with matching package identity, version, and SHA256, captured in the
home's immutable artifact layout. An archive `path` must be absolute or use
`${TOOLFRAME_HOME}`; a `url` must resolve to a captured local wheel. Relative archive
paths, sdists, directories, VCS sources, source subdirectories, and ambiguous
mixed source records are refused. Index records may retain sdist metadata when
their required wheels are available; maintenance never selects a source build.

The serialized metadata contains no raw composition, candidate configuration, source URLs,
dependency URLs, private keys, or token values. The snapshot files themselves can
contain credentials and business records: the operator's archive implementation
owns their access control and storage protection.

In protected operator memory, each `EnvironmentLock` also has a final `lock: str`
field containing the exact recorded pylock text. Its SHA256 must match `sha256`.
The field is excluded from dataclass `repr` and from `as_dict()`; it can contain
credential-bearing dependency URLs and must not be logged, displayed, or copied
to a public manifest. This narrow accessor lets a restore consumer rebuild exact
environments without importing Core's private journal layout.

## Supported state and refusal cases

Format 1 supports local SQLite Core state, business stores managed by the standard
`sql-sqlite` provider, and regular files within the application home. It
validates SQLite integrity and business-store application/owner identities with
read-only immutable connections. It requires an initialized Core schema version
1 or 2 and the composition lock recorded by that journal. It never adopts a new
storage binding from freshly edited desired configuration.

The context refuses:

- An occupied HostLock, a dirty/nonempty `worker_owners` marker, a live worker
  bearing a recorded host-run token, uncertain runtime cleanup, or an unavailable
  or malformed process inventory. A missing PID or PID file does not prove that
  cleanup finished. Historical plugin `stopped` flags are not liveness evidence.
- `unknown` or `failed` runtime observations, including observations without a
  PID. Only explicit durable `stopped` records and exact empty observations for
  disabled candidates with no current or previous selection are accepted as
  never-started observations. Core startup can
  rewrite disabled historical rows to `unknown`; such homes remain unsupported
  until Core produces a new durable worker-stop observation. Setting a manual
  plugin stopped flag alone does not supply that proof.
- Pending management operations or forward-repair work, unsupported or damaged
  journals, identity mismatches, missing retained private keys for existing
  stores, or credential files readable by another user.
- Nonempty SQLite WAL or rollback journals. Cleanly stop/reconcile through Core
  before retrying; the maintenance reader never checkpoints or discards pages.
- External PostgreSQL Core state, PostgreSQL/S3 or unknown provider-managed state,
  `separate_application` definitions, and uncertain/unbound business stores. The
  reader does not infer a complete backup from the presence of a local directory.
  Reserved `data/plugins/<owner>/data.sqlite` and its `-wal`, `-journal`, and
  `-shm` paths require a retained store binding before file-type handling, including
  when an unbound path is a directory or socket. Other attachment filenames and
  nested attachment paths remain ordinary snapshot files.
- Desired composition edits that have not been applied and cleanly stopped. A
  matching current composition, journal revision, and composition lock are
  required so new desired external state cannot bypass persisted-state checks.
  The normalized semantic composition must also match the journal, including
  application, plugins, bindings, runtime, packages, and access policies. Older
  records receive current model defaults. `TOOLFRAME_WHEELHOUSE` and
  `TOOLFRAME_OFFLINE=1` are applied read-only as Core applies them; run with the
  same package override environment as the source or expect refusal.
- Symlinks in snapshot state, paths escaping the home, nonregular state files,
  missing captured wheels, and modified immutable artifacts. Known regenerable
  worker environments are excluded, including their normal Python symlinks.
- Missing/malformed source `pyvenv.cfg`, an operator interpreter major/minor or
  implementation different from the recorded source environment, a different
  full version when the source records one, or lock markers incompatible with the
  inspection host. Version metadata must be numeric `major.minor` or
  `major.minor.patch`. Both `version_info` and `version` are checked when present;
  a full recorded version takes precedence in the inventory. When uv records
  only major/minor, no source patch version is inferred. Requires-Python and
  environment markers are checked against the inspector's full version.
  This validation supplies same-platform restoration metadata; cross-platform
  recovery is not guaranteed. Consumers must match the recorded version at its
  known precision and check the full recovery interpreter against the lock.
  Each required locked package must have a wheel compatible with the source
  interpreter/platform tags. Package markers may exclude a package only when
  they are valid and prove it is not required on that source platform.

Refusals use `toolframe_sdk.PluginError`. Stable maintenance error codes are
`maintenance_layout_invalid`, `maintenance_ownership_unproven`,
`maintenance_recovery_required`, `maintenance_external_state`, and
`maintenance_artifact_missing`. HostLock contention retains
`host_already_running`. Invalid runtime backend settings retain Core's existing
`core_state_config_invalid` error. Error messages do not expose persisted values
or process command output.

## Restoration into a new home

This interface supplies source validation and inventory; an operator backup
package owns archive creation, checksums, extraction, and standalone restoration.
Keep the application identity, composition, lock, Core/business stores, captured
artifacts, and retained keys together. Restore regular files under their relative
paths and retain permission modes. Restoration must run with Core stopped and
must not invoke plugin-based live restore or automatically migrate stores.

Host locks, endpoint information, sockets, SQLite shared-memory files, and empty
SQLite journals are omitted. `.toolframe/environments` and `.toolframe/tmp` are
regenerable caches and are omitted. The restore consumer must read retained lock
records through protected `EnvironmentLock.lock`, expand `${TOOLFRAME_HOME}` for local wheel
references, verify dependency hashes, and rebuild the corresponding backend/UI
environment paths before boot. Core's ordinary missing-environment preparation
recompiles artifact requirements; it does not automatically replay every retained
dependency lock. Copying an old virtual environment is not a relocation step.

Protected locks can also contain absolute source-home `packages.archive.path`
values. Restore consumers must validate those paths against the captured artifact
inventory and relocate them to the new home by the lock format's path semantics.
The maintenance reader preserves the exact recorded lock and never rewrites it.

The restore consumer must choose an absent final destination, create its own new
staging home, and keep the lock through destination finalization and environment
creation:

```python
from toolframe_core.maintenance import application_lock

with application_lock(staging_home):
    # Caller has verified final_home is absent and owns extraction/rename rules.
    staging_home.rename(final_home)
    # The same kernel lock still excludes another Core at final_home.
    # Expand only ${TOOLFRAME_HOME} in each protected EnvironmentLock to the
    # NEW final_home, then use its compatible interpreter for uv venv and
    # uv pip sync from the exact expanded lock before first boot.
    # Never log the lock text.
```

The module performs none of these restore writes or dependency downloads. Locking
a staging/final destination does not assert that an arbitrary existing
application there is stopped; the consumer owns destination freshness checks.

A successful inventory does not promise an air-gapped restart. Each
`dependency_wheels` entry with `path: null` requires the exact pinned wheel through
network access or an operator-supplied wheelhouse. The recorded hash must still be
verified during rehydration. Compatible Python and platform, Core/runtime preparation, and any
operator environment configuration are separate restoration prerequisites. The
maintenance module neither downloads dependencies nor captures external runtime
credentials. This distinction does not weaken the refusal of external Core,
business, or attachment state.

Run maintenance with the same runtime backend environment used to serve the
source application. An external Core journal is not discoverable through the
local SQLite layout, and a stale local journal must not be substituted by
omitting the application's external backend settings.
