# Operating Toolframe

The CLI, management API and Dashboard use the same durable operation engine.
Use plans to inspect a change before applying it, and operation IDs to inspect
or cancel work after it starts.

The native command is `toolframe`; the Python package entry point is
`toolframe-core`. Put `--home PATH` before the subcommand to select an application.
The native launcher's `--version` and `-V` flags print its version without
preparing Python or contacting a package index.

## Plugin sources and candidates

Plugins can come from these sources:

| Source | Form |
| --- | --- |
| Local directory | `path:/absolute/directory` |
| Git revision | `git+https://host/repo.git@ref#subdirectory=path` |
| Wheel | A wheel file or HTTPS wheel URL |
| Package index | `pypi:distribution==version` or a bounded version range |

Git refs resolve to full commits. Local edits receive new content identities even
when the package version stays the same. Workers execute captured wheels, not live
editable imports. Each wheel supplies one `toolframe.plugins` entry point and one
stable plugin ID.

`plugin add` installs and validates a package, leaving it disabled unless you pass
`--enable`. Validation reports capture/build, environment, import, configuration
and resource preflight separately. `plugin validate ID --runtime` exercises a
temporary application and store; it does not certify live business health.

`plugin update ID` resolves the source again and stages a candidate without
changing the active generation. Use it to pick up new wheel bytes or dependency
resolution, or pass `--source SOURCE` to change the requested source or version.
Named application recipes select compatible stable versions during initialization,
write exact sources and locks, and retain their original ranges for explicit
updates. See [application recipes](application-templates.md) for selection,
isolation and offline behavior.

Review and apply the update:

```sh
toolframe --home ./my-app plan --action reload --id plugin-id --json
# Replace PLAN_ID with result.plan_id from the completed plan.
toolframe --home ./my-app plugin reload plugin-id --plan-id PLAN_ID
toolframe --home ./my-app plugin status plugin-id --json --check
```

A plan binds the candidate content, composition revision, worker generations,
catalog and schema history. Changed inputs produce a stale-plan error. `reload`
does not enable a disabled plugin; use `enable` to restore its services, pages
and tools. Status distinguishes the installed candidate, active generation,
recent health, missing dependencies and last operation.

`enable` and `apply` reuse the latest installed candidate when its source matches
the composition, even after the original source or an older binary bundle has
been removed. They verify the retained wheel and rerun import/configuration
preflight. `reload` and `validate` also retain installed index selections,
including `pypi:package>=minimum,<ceiling` ranges. Local directories, wheel paths
and Git refs are captured afresh. A missing or modified retained wheel is an error
rather than an implicit package upgrade. Missing environments are restored from
their exact locks when the runtime profile is unchanged; required dependencies
must be cached or downloadable.

`plugin stop ID --force` explicitly terminates workers. A manual stop remains
fenced until `enable`, `reload` or `apply` explicitly resumes it. Normal reload
drains admitted work and requires cleanup before starting a replacement. See
[lifecycle and recovery](architecture.md#workers-reload-and-recovery) for drain
timeouts, failed activation and schema-repair rules.

## Application home and restart

The application home contains `toolframe.toml`, `toolframe.lock`, private
`.toolframe/` runtime state and persistent `data/`. Keep that home to retain the
application's identity, credentials and records. Keep private state and
credentials out of version control. Plugin source can live in a separate
directory; installing a wheel makes routine startup independent of the author's
virtual environment and build tools.

Once setup has prepared the application, run the official CLI from its home:

```sh
cd my-app
toolframe serve
```

The native launcher accepts `toolframe` with no arguments as well. `uvx toolframe`
is the equivalent tool-runner entry point. These commands select the current
directory; they do not search parent directories or hidden QA homes. For an
explicitly selected home, use `toolframe --home /path/to/app serve`. First-time
asset builds, provider installation and credential provisioning are setup work;
routine restart does not require a project-specific startup script or repeat setup.
Native runtime downloads on a fresh machine are described in the
[runtime guide](runtime.md).

Startup prints the selected home, configuration path, application identity and
Core version. Run `toolframe status --check` from that home to verify application
readiness, then check the intended page or client. A listening management server
does not by itself mean the business application is ready.

Starting from a directory without `toolframe.toml` or an existing local Core
journal fails with `application_not_initialized` (exit 2), before creating app
state or installing a skill. Run `toolframe init` in the intended new home, or
select the existing home with `--home`. An existing journal, or explicitly
configured PostgreSQL journal, retains its recovery path when configuration is
missing or invalid. The CLI marks that application not ready while keeping
management recovery available; repair the selected configuration before applying
changes. It does not silently choose a different application.

## Composition and local development

`toolframe.toml` holds the desired configuration. `toolframe.lock` records exact
artifacts and environments; the Core journal records actual generations,
candidates, interrupted operations and recovery information. CLI and API edits
compare revisions and replace TOML atomically. Use `--expected-revision` to
protect a managed edit against concurrent changes.

After editing TOML manually, run `plan`, review its result, then run
`apply --plan-id PLAN_ID`. Invalid TOML remains visible through management so
operators can diagnose and repair it.

`export --output PATH` writes a reviewable composition without overwriting an
existing file. Exports and management views redact schema-marked secrets and
credentials. Restore required secrets from the operator's original configuration
before applying an exported copy.

For local distributions, set `[packages] find_links=["/absolute/wheelhouse"]`.
`TOOLFRAME_WHEELHOUSE` provides the equivalent launcher/host override. Verified
Intel companion wheels do not add cache paths to `toolframe.toml`; explicit local
wheels with the same filename take precedence for plugin resolution. See
[runtime preparation](runtime.md#package-sources-and-companion-wheels).

Set `watch=true` on a local plugin to enable stable, debounced validation and
reload. Watch does not enable disabled or manually stopped plugins, migrate
existing data, force termination or restart Core.

## LAN access

The default listener is `127.0.0.1`. Override it for one run:

```sh
toolframe --home ./my-app serve --host 0.0.0.0
```

To save the listener when creating an application:

```sh
toolframe --home ./my-app init --template entry-register --host 0.0.0.0
toolframe --home ./my-app serve
```

For an existing application, set `host = "0.0.0.0"` under `[application]` in
`toolframe.toml` and restart Core. `serve --host` overrides that value for its
process without editing the composition. A specific LAN IP or IPv6 address is
also supported; use `--host ::` for all IPv6 interfaces. Host values must be IP
addresses or `localhost`, without a URL, brackets or port.

On another device, open `http://SERVER_LAN_IP:9876` using the server's numeric
address, not the wildcard bind address. `toolframe open` and management commands
use a connectable local address when Core binds a wildcard.
`.toolframe/endpoint.json` records the effective listener, including a temporary
`serve --host` override. Provider worker ports remain private loopback listeners
behind Core.

Host/Origin checks reject arbitrary DNS names and cross-origin browser requests.
Use the numeric IP for LAN access, or configure a canonical HTTPS origin as
described in [OIDC authentication](oidc-authentication.md). Dashboard passwords,
MCP bearer keys and explicitly published HTTP APIs have separate access rules;
llms documents remain public. See [authentication](authentication.md) and
[MCP authentication](mcp-authentication.md).

## Management API and operation history

Management requires direct local control, a bearer token/sign-in cookie or an
access password with the required system scopes. The management token is stored
in `.toolframe/management.token` with owner-only permissions.

| Interface | Contract |
| --- | --- |
| `/_toolframe/v1/status` | Core health, application readiness, candidates, observed generations and dependency/impact graph |
| `/_toolframe/v1/readiness` | HTTP 200 when required capabilities are ready; otherwise 503. Reports `ready` and optional `degraded` independently. |
| `/_toolframe/v1/operations` | GET history; POST `{ "action": "reload", "params": { "id": "plugin-id" } }` returns HTTP 202. |
| `/_toolframe/v1/operations/{id}` | Durable operation status; POST to `/_toolframe/v1/operations/{id}/cancel` requests cancellation at a safe boundary. |
| `/_toolframe/v1/composition` | TOML text and its revision |
| `/_toolframe/v1/services/{owner}/{service}/{method}` | Authenticated JSON business-service calls |
| `/healthz` | Public Core liveness, independent of application readiness |

Operation history uses permanent, increasing `sequence` numbers, newest first.
The Dashboard displays each number with its creation time in the browser's time
zone. It defaults to 10 rows per page, with 25 and 50 also available. Plugin,
action and result filters query the entire journal, including removed plugins.
Previews use labels such as `plan reload`. The Dashboard can reload its own
provider and reconnect while Core completes the operation.

`GET /_toolframe/v1/operations?page=1&page_size=10` returns `items`, `page`,
`pages`, `page_size`, `total`, `through`, `newer_count` and filter choices.
Optional `target`, `kind` and `status` filters combine with AND; empty `target=`
selects application-level operations. Page size is bounded to 1–100.

Send the returned `through` on subsequent pages to exclude newer arrivals until
an explicit refresh. Statuses remain current, so a changing status can alter the
filtered count. Out-of-range pages clamp to the last page. Requests without
query parameters return the legacy list of the latest 50 operations.

History reads require `system.plugins.read` and redact secrets. The Dashboard
uses the same query through `context.manage("operations", params)`. Reading or
filtering history never creates a journal operation.

## Automation and exit codes

Mutations support `Idempotency-Key`. Identical retries return the original
operation, even if it changed the composition revision. A different request with
the same key returns a conflict.

Client disconnect does not cancel host work. CLI `--no-wait` returns the operation
ID immediately; use `operation status ID` or `operation cancel ID` later.

Use `--json` for structured output and `status --check` when readiness should
affect the exit code:

| Code | Meaning |
| --- | --- |
| 0 | Success or accepted work |
| 2 | Input or configuration error |
| 3 | Validation or readiness failure |
| 4 | Runtime or recovery failure |
| 5 | Stale plan or conflict |
| 130 | Cancellation |
