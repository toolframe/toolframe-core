# Toolframe core


Toolframe is a persistent local control host for replaceable Python plugins.
Core owns composition, immutable package environments, worker lifecycles,
management operations and a durable Core journal. SQLite (`data/core.sqlite`) is
the default; Core also supports a dedicated PostgreSQL database through runtime
environment configuration. Dashboard, MCP, llms.txt, business
storage and Entry Register are independently packaged plugins.

The standard executable can start an empty host or an Entry Register application
with Dashboard, MCP, documentation, storage and access-password plugins.
Business data survives plugin replacement and removal. Plugins run trusted Python
code; worker isolation is not a sandbox for untrusted packages.

For PostgreSQL provisioning, startup and recovery, see
[PostgreSQL Core state](docs/postgresql-state.md). Both backends run one Core per
application and retain configuration, private keys and artifacts in its home.

Operator backup tools can use the public [offline maintenance interface](docs/offline-maintenance.md)
to hold the application lock and validate stopped local SQLite state.

Optional [OpenTelemetry tracing and logs](docs/tracing.md) follow HTTP and nested
plugin calls with application, plugin and function context. Telemetry is disabled
by default and adds SDK/exporter dependencies only when enabled; structured local
logging works independently, and log export requires its own opt-in.

## Quick start

Install the native CLI with [uv](https://docs.astral.sh/uv/getting-started/installation/):

```sh
uv tool install toolframe
toolframe --home ./my-app init --template entry-register
toolframe --home ./my-app serve
```

In another terminal, run `toolframe --home ./my-app open` for the local sign-in
link. `uvx toolframe ...` runs the same CLI without a persistent installation.
The [GitHub Release archives](https://github.com/toolframe/toolframe-core/releases)
contain the identical executable for installation directly on `PATH`.

For Docker, use the [container guide](docs/container.md) to initialize a persistent
application with the Debian or Distroless image. Both include uv, Python, Core
and Tini, and support Linux ARM64 and x86-64.

The executable contains no Python wheels. First use reuses a compatible uv or
prepares its verified fallback, installs Python 3.14.7 and `toolframe-core` from
PyPI, and resolves each selected plugin independently. Core requires
`fastapi-cordis==0.2.2`. `init` without `--template` creates an empty application;
the Entry Register template selects storage, Dashboard, MCP, documentation and
password providers. Plugin choices remain in the application configuration.
The named Entry Register recipe selects FastMCP 0.3.1 for new applications.
Explicit local TOML recipes retain their selected sources, and initialization
refuses to rewrite an existing application or its retained plugin artifacts.

Native CLI wheels target macOS 12+ on Apple Silicon and Intel, and Linux glibc
2.28+ on ARM64 and x86-64. Windows and musl Linux are outside this release.
Intel macOS automatically downloads a separately published cryptography wheel
from the matching GitHub Release and verifies its pinned SHA-256. First use needs
network access; `--offline` requires previously prepared assets. See
[runtime preparation](docs/runtime.md) for caches, uv selection and local wheels.

Pass `--home PATH` before the subcommand. `toolframe --version` and `-V` print the
launcher version without preparing Python or contacting a package index. Python
library users can install `toolframe-core` on Python 3.14+; plugin authors install
`toolframe-sdk`. The Python CLI entry point is `toolframe-core`.

For full HTML applications with forms, app-owned JavaScript/CSS and no Node build
environment, use `plugin new NAME --from pypi:toolframe-web==0.1.0 --template toolframe-web/notes` and follow
[Web applications](docs/web.md). Web and Streamlit can share one Core with explicit
presentation mounts.

## LAN access

The default listener is `127.0.0.1`. Bind all IPv4 interfaces for one run:

```sh
toolframe --home ./my-app serve --host 0.0.0.0
```

To save the address when creating an application:

```sh
toolframe --home ./my-app init --template entry-register --host 0.0.0.0
toolframe --home ./my-app serve
```

Existing applications can set `host = "0.0.0.0"` under `[application]` in
`toolframe.toml`, then restart core. `serve --host` overrides that value only for
the current process; it does not edit the composition. A specific LAN IP or IPv6
address is also supported; use `--host ::` for all IPv6 interfaces. Host values
are IP addresses or `localhost`, without a URL, brackets or port.

On another device, open `http://SERVER_LAN_IP:9876`. Dashboard business access is
public until a password is configured. For example, use the
server's actual `192.168.x.x` address, not the wildcard `0.0.0.0` bind address.
`toolframe open` and management CLI commands continue to use a connectable local
address when core binds a wildcard. The effective listener is recorded in
`.toolframe/endpoint.json`, including a temporary `serve --host` override.

MCP (`/mcp`), llms (`/llms.txt`, `/llms/...`) and explicitly published business
APIs are public. System management requires direct local control, a management
token or the corresponding password scopes. Host/Origin checks still reject arbitrary DNS
names and cross-origin browser requests; use the server's numeric IP for LAN
access. Provider worker ports remain private loopback listeners behind core.
The public `/healthz` endpoint continues to report core liveness.

For a canonical HTTPS origin, shared OIDC Dashboard login and revocable Agent
access, install the optional OIDC plugin and follow
[OIDC authentication](docs/oidc-authentication.md).

To repeat the real executable/LAN acceptance probe with this machine's IPv4
address (it creates and stops a separate temporary application):

```sh
uv run python scripts/verify-network.py \
  --binary target/release/toolframe \
  --address SERVER_LAN_IP --output build/lan-access.json
```

## Develop a custom plugin with an agent

`init` supplies the bundled `toolframe-plugin-design` skill at
`APP/.agents/skills/toolframe-plugin-design`. Open an agent session in that
application directory and invoke `$toolframe-plugin-design` with your business
requirements. Existing sessions may need to refresh skill discovery.

The skill covers domain/data design, public SDK services, managed SQL, optional
Web/Streamlit/MCP resources and installed validation. Core ships the guidance and
a minimal provider-free `toolframe-core/basic` template. Optional templates are
owned by their distributions and discovered from static wheel metadata:

```sh
toolframe --home APP template list
toolframe --home APP template list --catalog
toolframe --home APP plugin new greeting --json
# Select a matching local authoring wheel and an optional template:
toolframe --home APP template show toolframe-web/notes --from wheel:/path/to/toolframe_web-0.1.0-py3-none-any.whl --json
toolframe --home APP plugin new notes --from wheel:/path/to/toolframe_web-0.1.0-py3-none-any.whl --template toolframe-web/notes
```

The default is now basic, with one internal service, bounded schemas and a test;
it does not add SQL, Web, Dashboard or MCP. Select
`toolframe-streamlit-dashboard/notes` explicitly for the previous Notes example.
`--presentation web` and `--presentation dashboard` remain optional-template
aliases. See [distribution authoring](docs/authoring.md) for index sources,
parameters, local/catalog discovery, third-party manifests and compatibility.

Generation works without an initialized application or running provider. Optional
code executes only in the selected distribution's isolated authoring environment;
it is not installed into Core. Generation does not register, enable or mount a
runtime plugin. Core installation and basic generation do not acquire optional
presentation templates.

The output defaults to `APP/plugins/NAME`; `--output PATH` selects an exact
alternative directory. Existing files, directories and symlinks are refused with
exit 5. JSON returns the plugin/package/module identity, absolute path, status
and selected template metadata. Generated projects are user-owned and remain
unchanged across template or skill upgrades. Edit existing source directly.

Build basic/Dashboard source with `uv build --wheel`; prepare Web source with
`toolframe --home APP web prepare SOURCE`. Install required providers separately,
then use `plugin add`, `plugin validate --runtime` and `plugin enable` for the
business plugin. See [authoring](docs/authoring.md) for the complete workflow.

For an existing application, or to obtain a separate copy:

```sh
toolframe --home APP skill install
toolframe --home APP skill install --output ./toolframe-plugin-design --json
```

`--output` names the exact skill directory. Installation needs no running server,
application configuration or management token. The skill has its own semantic
version in the `SKILL.md` YAML frontmatter, currently `version: "0.5.0"`.
An installation receipt records that version and every shipped file's SHA-256.
The skill's top-level `evals/` directory is development-only: it stays in the
source repository and is excluded from the core wheel, installed copies and
`.toolframe-install.json`. Upgrading an unmodified older copy removes its bundled
evals and their receipt entries. Customized older copies retain the confirmation
and backup protections described below.

After a binary update, `serve` refreshes the bundled skill before starting core.
Updates affect only the installed skill; generated/customized business plugins
under `APP/plugins/` or an explicit output directory are never refreshed from it.
Unmodified copies update automatically. Modified, added or deleted files require
confirmation in an interactive terminal; the default answer keeps the local copy.
Noninteractive startup keeps customizations, prints how to resolve the conflict,
and continues serving. It does not repeatedly ask about a customized copy when the
binary still ships the same skill. `skill install` explicitly checks for replacement.

JSON/noninteractive `skill install` returns exit 5 and `confirmation_required: true`
when a decision is needed. After the user confirms, `skill install --overwrite`
applies the replacement and saves the customized copy as a sibling ZIP archive.
Use `--output` to compare or merge a separate copy instead. Destinations that are
files or contain symlinks are retained; choose a new directory for those cases.
The installer reports `installed`, `updated`, `unchanged`, or `preserved` and keeps
`.toolframe-install.json` as its baseline. Global agent settings are untouched.
The [testing guide](docs/testing.md) describes delivery, scaffolding and executable
checks, including the limits of structural skill validation.

## Install, inspect and replace plugins

```sh
toolframe --home ./example plugin add /absolute/path/to/plugin
# add installs and validates but leaves the plugin disabled; add --enable opts in.
toolframe --home ./example plugin validate plugin-id
toolframe --home ./example plugin validate plugin-id --runtime
toolframe --home ./example plugin enable plugin-id

toolframe --home ./example plugin update plugin-id
toolframe --home ./example plan --action reload --id plugin-id
# Review the affected members and copy result.plan_id from the completed plan.
toolframe --home ./example plugin reload plugin-id --plan-id PLAN_ID
toolframe --home ./example plugin status plugin-id --json --check
```

`enable` and `apply` reuse the latest installed candidate when its source matches
composition, including after the original source or an older binary bundle is
removed. They verify the retained wheel and rerun import/configuration preflight.
`reload` and `validate` likewise reuse an installed exact `pypi:package==version`
wheel; local directories, wheel paths, Git refs and unpinned index requests are
captured afresh. A missing or modified retained wheel is an error, never an
implicit package upgrade. Rebuilding a missing environment may require cached or
downloadable dependencies.

`update` explicitly resolves the source again and stages a candidate; it does not
change the active generation. Use it to pick up a replaced wheel or dependency
resolution, or to change the requested source/version. `reload` does not enable a
disabled plugin: use `enable` to restore its services, pages and tools.
A reviewed plan binds the
candidate content, desired revision, worker generations, catalog and schema
history. Changed inputs produce a stale-plan error instead of applying a different
candidate. `--expected-revision` protects managed edits to composition.

Supported sources are local directories (`path:/absolute/directory`), Git
(`git+https://host/repo.git@ref#subdirectory=path`), wheel files/HTTPS URLs and
`pypi:distribution==version`. Git refs resolve to full commits; local edits receive
new content identities even when their package version is unchanged. Workers use
captured wheels, never live editable imports. Each wheel supplies one
`toolframe.plugins` entry point and one stable plugin ID.

Validation reports capture/build, environment, import, configuration and resource
preflight separately. `--runtime` exercises a temporary application/store; it does
not certify live business health. Status distinguishes the installed candidate,
selected active generation, recent health, missing dependencies and last operation.

## Composition and data

`toolframe.toml` is the desired authority. `toolframe.lock` records exact artifacts
and environments. Core SQLite records actual generations, candidates, interrupted
operations and recovery information. CLI/API edits compare revisions and replace
TOML atomically. After editing TOML manually, run `plan`, then
`apply --plan-id PLAN_ID`; `export --output PATH` writes a reviewable copy without
overwriting an existing file. Schema-marked secret values and credentials are
redacted in exports and management views; restore required secrets from the
operator's original configuration before applying an exported copy.

Use `[packages] find_links=["/absolute/wheelhouse"]` for local distributions.
`TOOLFRAME_WHEELHOUSE` provides the equivalent launcher/host override. The native
launcher supplies verified Intel companion wheels without persisting cache paths
in `toolframe.toml`. Explicit local wheels with the same filename take precedence
for plugin resolution. Set
`watch=true` on a local plugin to opt into stable, debounced validation and safe
reload. Watch does not enable disabled or manually stopped plugins, migrate
existing data, force termination or restart core.

Business storage is a replaceable provider. Each plugin receives a managed SQL
and transaction client for its retained store. SQLite stores it at
`data/plugins/<id>/data.sqlite`; the PostgreSQL provider uses a plugin-owned schema
in the application database. Plugins access another owner's data through its
public business service. No raw connection is supplied through the SDK client.
Plugins remain trusted Python code, with access to their inherited environment.

SQLite and PostgreSQL providers can run together, with one retained store per
plugin. Use service defaults and per-plugin bindings to select each backend; see
[SQL storage configuration](docs/sql-storage.md). Binding changes do not migrate
existing data and cannot reassign a retained store.

```sh
toolframe --home ./example migrate plan plugin-id
toolframe --home ./example migrate apply plugin-id --plan-id PLAN_ID
```

Fresh empty stores can initialize automatically. Existing-data migrations require
explicit apply, a checked plan and a backup. A committed or uncertain schema
upgrade never triggers automatic old-code recovery. Status exposes
`forward_repair` until a compatible candidate is explicitly repaired/applied.
Disabling or removing a plugin retains its database.

## Lifecycle and management

Required dependents drain together. Admitted calls retain their generation and
transaction leases until completion. Drain timeout keeps intact old instances;
normal reload does not kill them. Worker/process-group cleanup must finish before
a replacement starts. With unchanged schema and proven cleanup, a failed new
cohort gets one attempt to restart the retained old snapshots. Its operation is
reported as `failed_recovered`, not successful.

`plugin stop ID --force` is the explicit termination path. A manual stop remains
fenced until enable/reload/apply explicitly resumes it. Core startup reconciles
surviving worker ownership and interrupted operations; invalid TOML is visible
through management rather than hiding the repair surface.

CLI, API and the Dashboard management page use the same durable operation engine.
The Dashboard shows a plan before applying lifecycle changes and can reload its
own provider; its UI session can reconnect while core completes the operation.

Operation history uses permanent, increasing `sequence` numbers, newest first.
The Dashboard displays each number with its creation time in the browser's time
zone, and defaults to 10 rows per page (also 25/50). Plugin, action and result
filters query the entire journal, including operations for removed plugins.
Previews are labeled with their planned action, such as `plan reload`.

`GET /_toolframe/v1/operations?page=1&page_size=10` returns an envelope containing
`items`, `page`, `pages`, `page_size`, `total`, `through`, `newer_count` and filter
choices. Optional `target`, `kind` and `status` filters combine with AND; empty
`target=` selects application-level operations. Page size is bounded to 1–100.
Send the returned `through` on subsequent pages to exclude newer arrivals until
an explicit refresh. Statuses remain current, so filtering by a changing status
can change the result count. Out-of-range pages clamp to the last page.
Requests without query parameters retain the legacy list of the latest 50
operations. All history reads require `system.plugins.read` and redact secrets.
The Dashboard uses the same query through `context.manage("operations", params)`;
reading or filtering history never creates a journal operation.

| Interface | Contract |
| --- | --- |
| `/_toolframe/v1/status` | Core health, application readiness, candidates, observed generations and dependency/impact graph. |
| `/_toolframe/v1/readiness` | HTTP 200 when required capabilities are ready; otherwise 503. Reports `ready` and optional `degraded` independently. |
| `/_toolframe/v1/operations` | GET history; POST `{ "action": "reload", "params": { "id": "plugin-id" } }` returns HTTP 202. |
| `/_toolframe/v1/operations/{id}` | Durable operation status; POST `/{id}/cancel` requests cancellation at a safe boundary. |
| `/_toolframe/v1/composition` | TOML text and its revision. |
| `/_toolframe/v1/services/{owner}/{service}/{method}` | Authenticated JSON business-service calls. |
| `/healthz` | Core liveness, independent of application readiness. |

Management accepts direct local control, a bearer token/sign-in cookie, or an
access password with the required system scopes. The management token remains in
`.toolframe/management.token` with owner-only permissions. The gateway defaults
to loopback and validates Host/Origin. Dashboard business access follows the
configured public/protected policy. MCP has an independent public/protected
policy with optional bearer keys; see [MCP authentication](docs/mcp-authentication.md).
The llms entrance remains public on LAN.
See [authentication](docs/authentication.md) for scope and fallback behavior.

Mutations support `Idempotency-Key`. Identical retries return the original
operation, even if that operation changed the composition revision. Different
requests using the same key return a conflict. Client disconnect does not cancel
host work. CLI `--no-wait` returns the operation ID immediately;
`operation status ID` and `operation cancel ID` manage it later.

CLI exit codes: 0 success/accepted work; 2 input/configuration; 3 validation or
readiness; 4 runtime/recovery failure; 5 stale/conflict; 130 cancellation. Use
`--json` for structured output and `status --check` when readiness should affect
the exit code.

## Repository boundaries

| Repository | Owns |
| --- | --- |
| `toolframe-core` | Persistent FastAPI/Cordis control host, gateway and thin Rust launcher. |
| `toolframe-sdk` | Public definitions, worker protocol, services, resources and scoped clients. |
| `toolframe-sql-sqlite` | Local owner databases, SQL authorization, transactions, receipts and migrations. |
| `toolframe-sql-postgres` | External plugin stores, provisioning, restricted roles, migrations and database backups. |
| `toolframe-streamlit-dashboard` | Native Streamlit pages, shared navigation and management UI. |
| `toolframe-fastmcp` | MCP tools and Streamable HTTP transport. |
| `toolframe-llms-txt` | Index composition and explicitly packaged Markdown assets. |
| `toolframe-demo-entry-register` | Ordinary business service, page, read-only tool, docs and template. |
| `toolframe-auth` | Password grants, sessions and authorization using its managed store. |
| `toolframe-dashboard-auth` | Password login and scoped password-management page. |

Core/SDK import no presentation provider. Workers have separate locked environments;
Streamlit page contributors share a separately resolved Dashboard environment.
An incompatible UI dependency set fails preflight. Replacing that shared process
can reset Dashboard session state. Catalog/schema changes replace MCP and require
client reconnection; backend-only changes can preserve MCP sessions.

## Build and verify

For pinned source validation and platform wheel builds, see [CI checks](docs/ci.md).
For PyPI, Trusted Publishers and GitHub Releases, see [releasing](docs/releasing.md).

Local development uses sibling checkouts of the coordinated repositories. Build
matching Python wheels into a development wheelhouse, then build the thin launcher:

```sh
uv sync --locked --group dev
bash scripts/dev-wheelhouse.sh
bash scripts/build-release.sh
uv run pytest -q
uv run ruff check src tests scripts
cargo test --locked
cargo fmt --check
uv run python scripts/verify-release.py \
  --binary target/release/toolframe --wheelhouse wheelhouse \
  --architecture arm64 --output build/release-arm64.json
```

`--wheelhouse` is an explicit local/offline input; no build step embeds its
contents into the executable. Release wheels use `scripts/build-launcher.py` and
Maturin. The same build exports the executable for GitHub Releases. Native Intel
builds also pin the externally supplied companion described in
[FastMCP's build guide](https://github.com/toolframe/toolframe-fastmcp#macos-intel-release-artifact).

The copied-executable probe covers empty and Entry Register applications,
Dashboard/MCP/llms services, online startup, retained data, reload and warm-cache
offline restart. Omit `--wheelhouse` after publication to test public PyPI
resolution. The SDK and providers have their own package checks; browser and
independent client checks are recorded separately in the [testing guide](docs/testing.md).

## Documentation

- [Object storage](docs/object-storage.md): local/S3 binary files and Image Gallery.
- [Runtime preparation](docs/runtime.md): installed uv reuse, overrides and offline startup.
- [Web applications](docs/web.md): source preparation, explicit roots, permissions and updates.
- [Architecture](docs/architecture.md): ownership, packages, lifecycle and data contracts.
- [Authentication](docs/authentication.md): access policy, scopes and recovery.
- [Dependency diagnostics](docs/dependency-graph.md): status and operation impact.
- [SQL storage](docs/sql-storage.md): simultaneous providers, ownership and migrations.
- [Glossary](docs/glossary.md): shared platform terminology.
- [Testing](docs/testing.md) and [CI](docs/ci.md): reproducible validation and release gates.
- [Changelog](CHANGELOG.md): development version changes.

## License

Toolframe Core and its Rust launcher are licensed under the Apache License,
Version 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

The bundled plugin authoring guide in
[`src/toolframe_core/bundled_skills/toolframe-plugin-design/`](src/toolframe_core/bundled_skills/toolframe-plugin-design/)
are licensed under [MIT](src/toolframe_core/bundled_skills/toolframe-plugin-design/LICENSE).
The basic template under `src/toolframe_core/basic_template/` is also MIT-licensed.
Generated plugins retain their template license; optional templates ship in their owning packages.

The package metadata uses `Apache-2.0 AND MIT` because the distribution includes
both the Apache-licensed Core and MIT-licensed guide/templates. Each license
applies to its respective files; this does not offer the Core under MIT.
Third-party dependencies retain their own licenses.

For development hooks and contribution rules, see [Contributing](CONTRIBUTING.md).
