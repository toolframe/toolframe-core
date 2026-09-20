# Build, install and validate

Use the application's actual executable and home. The examples use `toolframe`
on PATH; `--home APP` goes before the subcommand. Consult `--help` for the installed
version rather than assuming commands from another Toolframe generation.

## New source and application

Copy `assets/sample-plugin` from this skill to a separate development directory.
Rename the distribution, import package, entry-point key, plugin ID, service names,
page keys and resource references consistently. The example records immutable
notes; replace that behavior with the agreed design. A headless service can omit
the page, tools, docs, `ui` extra and storage when not needed.

Build with `uv build --wheel` from the plugin source. The starter contains no
sibling path dependencies. Where Toolframe packages are not in the configured
index, use an existing wheelhouse for dependency resolution. For a new local
application:

```sh
toolframe --home APP init
toolframe --home APP serve
```

Run `serve` in a separate terminal. Add required providers to the composition
before enabling a plugin that consumes their services. For a local SQLite wheel:

```sh
toolframe --home APP plugin add /path/to/toolframe_storage_sqlite-0.1.0-py3-none-any.whl --enable
toolframe --home APP plugin add /path/to/my-plugin
toolframe --home APP plugin validate my-plugin
toolframe --home APP plugin validate my-plugin --runtime
toolframe --home APP plugin enable my-plugin
toolframe --home APP plugin status my-plugin --check
```

Replace `my-plugin` with the definition's ID. `add` validates and leaves the plugin
disabled unless `--enable` is explicit. Default validation covers package,
dependencies, imports, configuration and resources. `--runtime` runs against a
temporary data environment; it does not establish production health or prove a
migration of existing data. Use sandbox configuration for plugins with external
side effects. Active health is checked after enable/reload.

`toolframe.toml` is the desired composition. CLI/API changes use the same core
operations; core SQLite records their actual state. For offline or unpublished
packages, use the launcher's `--wheelhouse PATH` and the composition's
`[packages].find_links`. Warm-cache `--offline` does not download missing assets.

## Local edits and reload

Editing source does not modify an active immutable snapshot, even if its package
version is unchanged. Validate and reload explicitly:

```sh
toolframe --home APP plugin validate my-plugin
toolframe --home APP plan --action reload --id my-plugin
toolframe --home APP plugin reload my-plugin --plan-id REVIEWED_PLAN_ID
toolframe --home APP plugin status my-plugin --check
```

The operation's `result.plan_id` identifies the reviewed plan. Recompute a stale
plan instead of replaying it. `plugin update ID --source PATH` stages a different
source; reload performs the switch. Operations normally wait; `--no-wait` returns
an operation ID for `operation status ID`.

Normal drain timeout aborts replacement and retains the old runtime. If activation
fails and cleanup/schema conditions permit it, core tries the retained old
snapshot once; `failed_recovered` still means this reload failed. Inspect candidate
status, active status and `plugin logs ID`, not only the installed version. When
the plan reports `restart_required`, follow that diagnosis instead of forcing a
reload. Dashboard sessions may reconnect; MCP catalog/schema updates require
client reconnection. Do not retry an uncertain business write automatically.

## Existing-data schema changes

Append a migration and stage the candidate. Then:

```sh
toolframe --home APP migrate plan my-plugin
toolframe --home APP migrate apply my-plugin --plan-id REVIEWED_MIGRATION_PLAN_ID
```

Review the plan's affected cohort, schema change and backup/recovery implications.
Apply only within the user's authorization to change that application's data;
finish the migration code and reviewable plan before requesting any still-missing
authorization. The explicit apply operation performs the schema change and runtime
switch. An ordinary reload, including watch mode, cannot substitute for it. A
committed schema upgrade is not automatically undone when new code fails; preserve
the database and perform forward repair or an explicitly chosen restore.

## Evidence for completion

Verify business invariants through public services and requested interfaces.
Record preflight, scratch runtime and active health separately. For persistent
plugins, create synthetic data in a disposable application, reload after a real
local edit, and show that the behavior updates while old records remain. Check
absence of optional providers where headless use is required. Test each requested
page/tool/docs surface and disclose any unavailable checks; do not count a schema
or package build as a successful user workflow.
