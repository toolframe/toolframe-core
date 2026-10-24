# Build, install and validate

Use the application's actual executable and home. The examples use `toolframe`
on PATH; `--home APP` goes before the subcommand. Consult `--help` for the installed
version rather than assuming commands from another Toolframe generation.
These examples target Core 0.8.0, SDK 0.4.0+web.1 and Web 0.1.0.

## New source and application

For a new plugin, generate the source with the installed CLI:

```sh
toolframe --home APP plugin new customer-notes --json
# For a new HTML Web app (recommended for unspecified forms/lists):
toolframe --home APP plugin new customer-notes --presentation web --json
# Alternative: the exact destination directory, relative to the current directory.
toolframe --home APP plugin new customer-notes --output ./custom/customer-notes
```

Choose one of these destinations. The default is `APP/plugins/customer-notes/`.
The CLI sets distribution `toolframe-customer-notes`, module
`toolframe_customer_notes`, plugin ID `customer-notes` and service namespace
`customer-notes.*`. JSON returns `plugin_id`, `package_name`, `python_module`,
`path` and `status: "created"`. An existing file, directory or symlink is refused.
IDs ending in `-` keep their plugin ID but add `plugin` to the distribution's end
to form a valid package name; the module uses its underscores form. Read the
returned names when editing references.
An existing plugin is edited directly, never regenerated or overwritten.

Generation uses the template bundled inside core, without needing this skill to
be installed or a Toolframe source checkout. It does not build, install, register,
enable or start a plugin, read application configuration, or request a management
token. The launcher may prepare its own core runtime on first use as usual.
Generated plugins are independent source trees; skill/binary updates leave them
alone. `plugins/` is an authoring location, not an auto-discovery mechanism.

Customize the generated files for the agreed design. The Dashboard Notes example provides
managed SQLite, capture/list services, a Streamlit page, a read-only MCP list tool
and llms documentation. A headless service can omit the page, tools, docs, `ui`
extra and storage when not needed. Keep its package and resource references
consistent when intentionally changing identity.

Choose the build path by the plugin's presentation:

- Dashboard/headless source without a Web recipe: `uv build --wheel` from its
  source directory, or let `plugin add SOURCE` build it.
- Web source: `toolframe --home APP web prepare SOURCE`; install the wheel path
  printed by that command. `plugin add SOURCE` and source reloads also perform
  Web preparation. A plain `uv build` does not prepare `web-assets.toml`.

The Web Notes starter has bounded list/create/update contracts and a pages-first
FSD presentation. It does not publish MCP tools or public HTTP methods. Read
[web-authoring.md](web-authoring.md) for its mounts, library choices and asset
allowlist; read [plugin-api.md](plugin-api.md) for optional private MCP resources.
Neither starter contains sibling path dependencies. Where Toolframe packages are not in the configured
index, use an existing wheelhouse for dependency resolution. For a new local
application:

```sh
toolframe --home APP init
toolframe --home APP serve
```

Run `serve` in a separate terminal. Add required providers to the composition
before enabling a plugin that consumes their services. For a local SQLite wheel:

```sh
toolframe --home APP plugin add /path/to/toolframe_sql_sqlite-0.4.0-py3-none-any.whl --enable
toolframe --home APP plugin add APP/plugins/customer-notes
toolframe --home APP plugin validate customer-notes
toolframe --home APP plugin validate customer-notes --runtime
toolframe --home APP plugin enable customer-notes
toolframe --home APP plugin status customer-notes --check
```

Use your generated path and ID in place of `customer-notes`. `add` validates and leaves the plugin
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

In the commands below, replace `my-plugin` with the definition's ID.
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

## Protected browser and MCP clients

Choose providers explicitly. `toolframe-auth` owns browser password grants and
sessions; `toolframe-dashboard-auth` supplies the login/password-management UI.
They use `toolframe-sql-sqlite`. MCP uses separate providers and credentials:

```sh
toolframe --home APP plugin add pypi:toolframe-fastmcp==0.3.0 --enable
toolframe --home APP plugin add pypi:toolframe-mcp-auth==0.3.0 --enable
```

The plugin IDs are `fastmcp` and `mcp-auth`. Configure protected `mcp_access`
before exposing confidential tools, as shown in [plugin-api.md](plugin-api.md).
The installed Dashboard Auth UI offers **Access passwords** and **MCP keys**.
For a CLI-only workflow, use these public HTTP APIs on the application's gateway:

| Request | Body / result |
| --- | --- |
| `POST /_toolframe/v1/auth/catalog` | `{}` returns browser scopes and the current `revision`. |
| `POST /_toolframe/v1/auth/create` | `{label, scopes, all_business: false, expires_at: null, expected_revision}`; omit `password` to generate one. Read the returned password once. |
| `POST /_toolframe/auth/login` | `{password}` with `Accept: application/json`; retain the response cookie for Web and protected service calls. |
| `POST /_toolframe/v1/mcp-auth/catalog` | `{}` returns MCP business scopes and provider/policy status. |
| `POST /_toolframe/v1/mcp-auth/create` | `{label, scopes, expires_at: null}` returns `{key, token}`; `token` is the one-time bearer secret. |
| `POST /_toolframe/v1/mcp-auth/list` | `{}` returns safe key metadata. `revoke` at the same prefix accepts `{id}`. |

Management calls use `Authorization: Bearer <operator-token>` from the owning
application's `APP/.toolframe/management.token`, or a browser session with the
required system grant. Read secrets from files without echoing them; store any
test credentials in an owner-only file. Refresh the catalog revision before each
browser-grant mutation. These are management endpoints, not business services.
There is no generic `GET /_toolframe/v1/services` directory endpoint.

Call a declared business method with object JSON at
`POST /_toolframe/v1/services/OWNER/SERVICE/METHOD`, where `SERVICE` is its declared
name (for example `customer-notes.notes`, without `@1`). Browser sessions carry
their own method scopes. Connect an actual MCP client to `/mcp` with
`Authorization: Bearer <MCP-token>` on every request. A Dashboard password or
operator token cannot substitute for an MCP token. Verify anonymous rejection,
reader discovery/call limits and writer mutations through the real client; then
read the same record from Web. Keep all sensitive methods `public_api=False`.

## Evidence for completion

Verify business invariants through public services and requested interfaces.
For Web, verify complete pages and fragments, both form encodings and JSON,
reader/writer controls, direct service denial, CSRF and stale-page rejection,
and declared assets at root and non-root mounts. Test the requested library
recipes in a browser. For coexistence, select both mounts explicitly, then use
Web and Dashboard to read the same business record. Web preparation must work
without Node/npm; installed wheels must work without the author source tree.
For a Dashboard plugin, also verify a read-only password cannot write, including a
direct protected service call. Check anonymous, reader, writer and local operator
access separately through Dashboard/service calls, explicit public HTTP methods,
and an actual MCP client. In a disposable application, disable the configured
browser credential verifier (`access.auth_provider`, normally `auth`) and verify
closed policy denies protected calls, public HTTP remains
intentional, and protected MCP keeps its independent gate. Restore auth, revoke a
grant, and verify denial and recovery. Back up configuration/data before testing
failure or reload in an existing application. Scope tests do not prove row isolation.
Record preflight, scratch runtime and active health separately. For persistent
plugins, create synthetic data in a disposable application, reload after a real
local edit, and show that the behavior updates while old records remain. Check
absence of optional providers where headless use is required. Test each requested
page/tool/docs surface and disclose any unavailable checks; do not count a schema
or package build as a successful user workflow.
