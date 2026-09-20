# Toolframe core

Toolframe is a persistent local control host for replaceable Python plugins.
Core owns composition, immutable package environments, worker lifecycles,
management operations and `data/core.sqlite`. Dashboard, MCP, llms.txt, business
storage and Entry Register are independently packaged plugins.

## Run the local workspace

The seven Toolframe repositories and `fastapi-cordis` must be siblings.
Python 3.14.7, uv 0.12.8 and Rust are the tested build tools.

```sh
# From toolframe-core:
uv sync --locked --group dev
bash scripts/build-release.sh

# An explicit template installs the four providers and the ordinary demo plugin.
./target/release/toolframe --home ./example init --template entry-register
./target/release/toolframe --home ./example serve
```

In another terminal, run
`./target/release/toolframe --home ./example open` for the local sign-in link.
`init` without `--template` creates an empty core. `serve` reconciles the selected
composition and keeps CLI/API diagnostics available during plugin preparation.
The default standard release embeds all eight Toolframe/Cordis wheels, including
the four providers and Entry Register. Copy the executable to another directory
or computer of the same architecture; no source checkout or separate wheelhouse
is needed. First use downloads the pinned uv/Python runtime and public Python
dependencies. This is not a fully offline cold-start distribution.

```sh
# From the directory containing a downloaded/copied executable:
./toolframe --home ./my-app init --template entry-register
./toolframe --home ./my-app serve
# In another terminal:
./toolframe --home ./my-app open
```

Plain `init` still creates an empty application. Bundling makes plugins available;
the chosen composition controls installation and activation. The default build
also writes `dist/toolframe-0.2.0-standard-aarch64-apple-darwin` (or the selected
target's equivalent). `--bundle core` builds the smaller three-wheel variant.

The commands below use `toolframe` for the built executable. Pass `--home PATH`
before its subcommand. The Python development entry point is
`uv run toolframe-core --home PATH ...`. `toolframe --version` (or `-V`) prints the
launcher version without preparing Python or contacting the package index.

## Develop a custom plugin with an agent

`init` supplies the bundled `toolframe-plugin-design` skill at
`APP/.agents/skills/toolframe-plugin-design`. Open an agent session in that
application directory and invoke `$toolframe-plugin-design` with your business
requirements. Existing sessions may need to refresh skill discovery.

The skill covers domain/data design, public SDK services, managed SQLite,
optional Streamlit/MCP/llms resources and validation/reload. Its references and
buildable Notes template ship inside the core wheel and Rust bundle; no Toolframe
source checkout is required. Create source with the same CLI the agent uses:

```sh
toolframe --home APP plugin new customer-notes
# Or select an exact destination and get machine-readable output:
toolframe --home APP plugin new reading-tracker --output ./plugins/reading-tracker --json
```

The default is `APP/plugins/<plugin-id>/`. `--output` is the complete destination,
relative to the current directory when not absolute. Existing files, directories
and symlinks (including dangling links) are refused with exit 5. IDs follow the
existing lowercase slug rule: `[a-z][a-z0-9-]{0,63}`.

For `customer-notes`, the distribution is `toolframe-customer-notes`, Python module
is `toolframe_customer_notes`, entry-point key and plugin ID are `customer-notes`,
and services use `customer-notes.*`. JSON returns `plugin_id`, `package_name`,
`python_module`, the absolute `path`, and `status: "created"`.
For an ID ending in `-`, the distribution adds `plugin` at the end so it remains
a valid Python package name (for example `a-` → `toolframe-a-plugin`); the module
uses that distribution with hyphens changed to underscores. The plugin ID is kept.

Generation needs no initialized application, installed skill, server or management
token. It creates source only: no wheel build, plugin dependency installation,
composition edit, registration or activation. The launcher still prepares its
own core runtime on first use when necessary. `plugins/` is not scanned or loaded
automatically.

Customize the generated service, schema, page, read-only MCP tool and llms document
for the business requirements; remove optional surfaces you do not need. Existing
plugins are edited directly, never regenerated. Build from the generated directory
with `uv build --wheel`. With core running and required providers installed:

```sh
toolframe --home APP plugin add APP/plugins/customer-notes
toolframe --home APP plugin validate customer-notes
toolframe --home APP plugin validate customer-notes --runtime
# Explicit activation after validation:
toolframe --home APP plugin enable customer-notes
```

The Notes starter requires the SQLite storage provider; install and enable it
before runtime validation/activation. `add` captures and builds the local source,
validates it, and leaves it disabled by default. See the operations reference for
provider installation, wheelhouse configuration, reload and explicit migrations.

For an existing application, or to obtain a separate copy:

```sh
toolframe --home APP skill install
toolframe --home APP skill install --output ./toolframe-plugin-design --json
```

`--output` names the exact skill directory. Installation needs no running server,
application configuration or management token. The skill has its own semantic
version in the `SKILL.md` YAML frontmatter, currently `version: "0.2.0"`.
An installation receipt records that version and every shipped file's SHA-256.

After a binary update, `serve` refreshes the bundled skill before starting core.
Updates affect only the installed skill; generated/customized business plugins
under `APP/plugins/` or an explicit output directory are never refreshed from it.
Unmodified copies update automatically. Modified, added or deleted files require
confirmation in an interactive terminal; the default answer keeps the local copy.
Noninteractive startup keeps customizations, prints how to resolve the conflict,
and continues serving. It does not repeatedly ask about a customized copy when the
binary still ships the same skill. `skill install` explicitly checks for replacement.

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

`update` stages a candidate; it does not change the active generation. Ordinary
`reload` can also capture the current source directly. A reviewed plan binds the
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
`TOOLFRAME_WHEELHOUSE` provides the equivalent launcher/host override. Standard
release wheels are an automatic fallback; local wheelhouse files with the same
filename override the bundled copy. Bundle cache paths are not written into
`toolframe.toml`. Set
`watch=true` on a local plugin to opt into stable, debounced validation and safe
reload. Watch does not enable disabled or manually stopped plugins, migrate
existing data, force termination or restart core.

SQLite storage is a provider. Each business plugin receives a managed SQL and
transaction client for its own retained `data/plugins/<id>/data.sqlite`. Plugins
access another owner's data through its public business service. No raw database
path or connection is supplied. This is ownership isolation for trusted local
plugins, not an operating-system sandbox.

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

| Interface | Contract |
| --- | --- |
| `/_toolframe/v1/status` | Core health, application readiness, candidates and observed generations. |
| `/_toolframe/v1/readiness` | HTTP 200 when required capabilities are ready; otherwise 503. Reports `ready` and optional `degraded` independently. |
| `/_toolframe/v1/operations` | GET history; POST `{ "action": "reload", "params": { "id": "plugin-id" } }` returns HTTP 202. |
| `/_toolframe/v1/operations/{id}` | Durable operation status; POST `/{id}/cancel` requests cancellation at a safe boundary. |
| `/_toolframe/v1/composition` | TOML text and its revision. |
| `/_toolframe/v1/services/{owner}/{service}/{method}` | Authenticated JSON business-service calls. |
| `/healthz` | Core liveness, independent of application readiness. |

Management uses a bearer token or local sign-in cookie; the token is stored in
`.toolframe/management.token` with owner-only permissions. Listeners are loopback
only and validate Host/Origin. Dashboard access is authenticated. Enabled MCP and
llms entrances are local provider endpoints at `/mcp` and `/llms.txt`.

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
| `toolframe-storage-sqlite` | Owner databases, SQL authorization, transactions, receipts and migrations. |
| `toolframe-streamlit-dashboard` | Native Streamlit pages, shared navigation and management UI. |
| `toolframe-fastmcp` | MCP tools and Streamable HTTP transport. |
| `toolframe-llms-txt` | Index composition and explicitly packaged Markdown assets. |
| `toolframe-demo-entry-register` | Ordinary business service, page, read-only tool, docs and template. |

Core/SDK import no presentation provider. Workers have separate locked environments;
Streamlit page contributors share a separately resolved Dashboard environment.
An incompatible UI dependency set fails preflight. Replacing that shared process
can reset Dashboard session state. Catalog/schema changes replace MCP and require
client reconnection; backend-only changes can preserve MCP sessions.

## Build and verify

The standard Rust executable embeds core, SDK, the independent Cordis 0.2.0 wheel,
SQLite, Streamlit Dashboard, FastMCP, llms.txt and Entry Register. The Intel build
also embeds the pinned native cryptography wheel. Only core/SDK/Cordis are installed
into the core environment; providers remain independently resolved workers.
It prepares the exact Python 3.14.7 architecture and invokes Python core. The
source for the pinned local Cordis wheel is `../fastapi-cordis`, whose `main`
includes the reactive lifecycle release `25a870f`. Bundled files are extracted
as an immutable content-addressed set and checked before use. Provider updates
can still come from a local wheelhouse or explicit plugin source without rebuilding
the launcher. `scripts/dev-wheelhouse.sh` remains available for that development
workflow and for the smaller `bash scripts/build-release.sh --bundle core` build.

```sh
uv run pytest -q
uv run ruff check src tests scripts/verify-release.py
cargo test
cargo fmt --check
uv run python scripts/verify-release.py \
  --binary target/release/toolframe \
  --architecture arm64 --output build/release-arm64.json
uv run python scripts/verify-scaffold.py \
  --binary target/release/toolframe --wheelhouse wheelhouse \
  --output build/scaffold-arm64.json
```

The integration tests expect the sibling repositories and check private-import
boundaries. Run each provider/SDK repository's `uv run pytest -q` as well. The MCP
repository has an additional `scripts/verify-gateway.py` acceptance driver.
Its `scripts/verify-codex.py` probes a running local demo through the installed
Codex app-server MCP client; see the CLI/Codex report for its tested approval and
client-interface scope.

The standard release probe copies the executable to a disposable directory,
clears inherited wheelhouse settings, and uses fresh uv/Python caches. It checks
empty core and the complete template, online startup and warm-cache offline
restart. Pass `--wheelhouse PATH` only when testing external-wheelhouse builds.

For Intel, first build the companion native dependency wheel documented in
`../toolframe-fastmcp/README.md` into `wheelhouse/`, then run
`bash scripts/build-release.sh --target x86_64-apple-darwin`. The matching release
probe uses `--binary target/x86_64-apple-darwin/release/toolframe` and
`--architecture x86_64-rosetta` on Apple Silicon.
Use `--native-wheelhouse PATH` during the build when the companion wheel is stored
elsewhere. Missing native wheels fail the build with preparation instructions.
Only the resulting standard executable is needed on the destination computer.

First preparation may download Python/dependencies. `--offline` uses exact cached
content and fails when required assets are missing. Native arm64 and x86_64 under
Rosetta were exercised on macOS 14.8.5. Physical Intel, the macOS 12 minimum,
independent desktop MCP clients and remote CI/publication remain explicit release
gates in the acceptance report. Linux/Windows, PostgreSQL, multiple instances,
artifact storage and full business audit providers are outside this version.
