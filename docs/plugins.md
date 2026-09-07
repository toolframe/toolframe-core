# Plugins in Toolframe

Toolframe uses `fastapi-cordis` to manage services and domain availability in its
single Python process. A plugin is a named service with a setup function, typed
configuration, declared dependencies, and cleanup callbacks. API requests, MCP
tools, Streamlit widgets and local `invoke` use the same lifecycle rules.

The current unreleased 0.2.0 package comes from `../fastapi-cordis-reactive`,
branch `codex/reactive-plugin-runtime`. The older `../fastapi-cordis` checkout
remains independent. Toolframe embeds an exact source copy and wheel, so deployed apps do not
need that sibling directory, a Node runtime, or an unpublished PyPI dependency.
This is an independent Cordis-inspired implementation; JavaScript Cordis plugins
and APIs are not interchangeable with it.

## What is managed

Every existing domain automatically gets a plugin named `domain.<domain_name>`.
Stopping that plugin prevents new calls to its MCP tools and new renders of its
widgets. Tools remain listed in the startup MCP catalog and report the structured
`plugin_unavailable` error when called. Widgets display an unavailable message on
their next render. An already rendered browser page is not forcibly refreshed.

`toolframe.database` is the stable typed database service. Its concrete plugin is
`toolframe.database.sqlite` or `toolframe.database.postgresql`, selected at startup.
`toolframe.artifacts` exposes artifact storage. These host services cannot be disabled or reconfigured using management
routes. Business domain exports and database migrations keep their existing
ownership rules. Stopping a domain never drops its tables or removes its files.

## Develop one business capability

A complete new business capability normally supplies one business service plugin.
Its domain still owns the business language, model, schema, tools, widgets and
peer documentation. Entities and individual functions remain ordinary model code;
small changes extend the existing capability. A local plugin needs no separate
repository or PyPI package.

Start from the [complete Stockroom example](../skills/toolframe-business-app/assets/business-plugin/README.md).
It includes the service, app catalog, schema variants, human form, two MCP tools
and handoff documents. Its four identities have different jobs:

| Identity | Example | Responsibility |
| --- | --- | --- |
| Public ServiceKey and Protocol | `stockroom.service` / `Stockroom` | Business methods available to consumers |
| Concrete Plugin | `stockroom.local` | Configuration, setup, dependencies and owned resource cleanup |
| Domain/schema owner | `stockroom` | Business source and durable tables/history |
| Automatic host gate | `domain.stockroom` | Availability of that domain's MCP tools and widgets |

Define the contract/key in `domains/stockroom/model/contract.py` and export them
from `domains/stockroom/__init__.py`. Keep the implementation in `model/service.py`
and its Plugin definition in `model/plugin.py`. Setup requires `DATABASE` when
the service persists data; local domain schemas are prepared before it starts.
Register and connect it in the composition root:

```python
from domains.stockroom.model.plugin import PLUGIN

PLUGINS = [PLUGIN]
DOMAIN_PLUGINS = {"stockroom": (PLUGIN.id,)}
SCHEMAS = {}  # The local domain's schema.json is discovered automatically.
```

Do not author another `domain.stockroom` plugin or leave the automatic gate
disconnected from the real service. Add extra dependencies to `app/pyproject.toml`
only when the capability needs them, then restart. MCP tools hold
`use_plugin(STOCKROOM)` and widgets hold `context.plugins.lease(STOCKROOM)` while
calling the same business methods. Persistence and idempotency stay inside that
service. Register cleanup for resources it opens; do not close host resources.

Across domains, import the key/Protocol from the owner's root public API.
A consumer retaining another service declares `requires=(STOCKROOM,)` and obtains
it with `ctx.require(STOCKROOM)` during setup. A call-time consumer holds a lease
for the whole operation; a public convenience function owns its lease internally.
Bind its domain to its actual service dependencies. Cross-domain deep imports
into model, tools, UI or SQL remain forbidden.

Finish with check/startup, one human and one MCP interaction, rejected invalid
input, idempotent replay where relevant, and stop/recovery with retained data.
The existing Entry Register app can still run through its automatic domain gate;
this default guides new complete capabilities without forcing a plugin per old
function or entity.

## Add a supporting service plugin

Create the optional `app/plugins.py` (newly scaffolded apps already include it):

```python
from fastapi_cordis import Plugin, PluginContext
from pydantic import BaseModel, ConfigDict

class FormatterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prefix: str = "Report"

async def formatter(ctx: PluginContext):
    # A real client would register ctx.on_dispose(client.aclose) immediately
    # after opening the connection. Cleanup is awaited on stop and failed setup.
    return lambda title: f"{ctx.config.prefix}: {title}"

PLUGINS = [
    Plugin("formatter", formatter, config_model=FormatterConfig),
]

# A domain keeps all these services alive during its tools/widgets.
DOMAIN_PLUGINS = {"entry_register": ("formatter",)}
```

Use a regular Python list or tuple for `PLUGINS`. A service may declare another
service with `Plugin(..., requires=("storage",))`, then use
`ctx.require("storage")` during setup. Setup returns the service value. Cleanup
callbacks run in reverse registration order; dependents stop before providers.

You can import `Plugin` definitions from installed third-party Python packages
instead of defining them in the catalog. List those packages in the existing
`app/pyproject.toml` dependencies and restart. Plugin code is trusted local Python
with the same privileges as other Toolframe source. The catalog must only define
plugins at import time; open connections and start workers in setup.

Names beginning with `toolframe.` and `domain.` are reserved. Duplicate names,
unknown dependencies/domains, dependency cycles, invalid configuration, and
enabled plugins depending on disabled plugins stop startup. Toolframe explicitly
calls `manager.validate()` for strict startup/preflight; the generic runtime alone
permits incomplete graphs to wait in `blocked` state.

## Use a service

When callers must work with interchangeable implementations, declare a
runtime-checkable Python Protocol and `ServiceKey[YourProtocol]`. Providers use
`Plugin(..., provides=KEY)`; consumers use `requires=(KEY,)`, `ctx.require(KEY)`
and `manager.lease(KEY)`. A service key has one selected provider. The manager
validates its contract and tracks the concrete provider's dependencies and leases.
See the [typed database contract](database-providers.md) for a working example.

In a synchronous MCP tool:

```python
from fastmcp.tools import tool
from toolframe_plugins import use_plugin

@tool
def entry_register_format_title(title: str) -> str:
    """Format one entry title using the configured formatter."""
    with use_plugin("formatter") as format_title:
        return format_title(title)
```

In a widget:

```python
def render(context):
    with context.plugins.lease("formatter") as format_title:
        st.write(format_title("Entries"))
```

Use a service only while its `with` block is open. Do not store the service in
session state, a global variable, or an untracked background task. Existing
`get_database`, artifact and idempotency helpers remain available for import-time
configuration compatibility; runtime work should go through managed tool/widget
entry points. Direct calls to domain Python functions outside those entry points
must hold their own leases if they need plugin availability/cleanup guarantees.

The lease is held inside the actual synchronous tool function, not just its
waiting HTTP request. If a timeout abandons a worker thread, its resources stay
alive until that thread exits. Background workers owned by a plugin must register
cooperative cancellation and joining as cleanup. Never hold a lease while awaiting
an operation that stops that same plugin; it will wait for you and time out.

## Operate the running app

Open `/info` for state, resolved dependencies, in-flight counts, lifecycle timings,
generation/config revision and owned-task diagnostics. Management actions follow
the actual state:

| State | Meaning | Recovery |
| --- | --- | --- |
| `active` | Accepting work | Stop drains current calls and pauses dependents. |
| `disabled` | Explicitly switched off | Enable sets the intent to run. |
| `blocked` | Enabled, waiting for a dependency | Restore `blockedBy` dependencies; consumers resume automatically. |
| `failed` | Setup or cleanup failed | Correct the cause/configuration, then Retry. Enable does not retry failures. |

Ordinary Stop preserves consumers' enabled intent. **Stop with dependents**
(`cascade=true`) switches off the whole dependent group; enable providers and
then those consumers explicitly. Host database/artifact plugins remain protected.
The HTTP interface also supports configuration previews and updates:

```sh
curl http://127.0.0.1:8000/api/plugins
curl -X POST http://127.0.0.1:8000/api/plugins/domain.entry_register/disable
curl -X POST http://127.0.0.1:8000/api/plugins/domain.entry_register/enable
curl -X POST http://127.0.0.1:8000/api/plugins/formatter/config/plan \
  -H 'Content-Type: application/json' -d '{"prefix":"Summary"}'
curl -X PUT http://127.0.0.1:8000/api/plugins/formatter/config \
  -H 'Content-Type: application/json' -d '{"prefix":"Summary"}'
curl -X POST 'http://127.0.0.1:8000/api/plugins/formatter/disable?cascade=true'
curl -X POST http://127.0.0.1:8000/api/plugins/formatter/retry
```

These endpoints use Toolframe's existing trusted-LAN access and host/origin
checks. Responses omit configuration values and service objects. Changes apply
to the running process only. Put durable defaults in `app/plugins.py`; a restart
loads those defaults again. After a cascade, enable providers before dependents.

Configuration replaces the complete settings object. The `/info` editor starts
blank and never fetches existing values. Preview validates input and reports
`changed`, `affected`, `protected`, `canApply`, `managerRunning`, `generation` and
`configRevision`; it runs no setup or cleanup. Preview is advisory: Apply validates
again and recomputes affected/protected services under the runtime mutation lock.
A normalized equivalent configuration is a no-op, preserving generations and
revision counters. Changed configuration drains/restarts active dependents; a
protected participant prevents application. If setup fails, its attempted config
remains and consumers become blocked. Correct the configuration, then Retry the
failed plugin; a config update alone does not clear failed state. External side
effects are not rolled back. Snapshots/plans omit configuration and service values;
`provides` can be a string or list, and `requires` contains resolved plugin IDs.

Stopping waits up to 30 seconds for tracked work. If work remains, the operation
fails with HTTP 409, restores availability, and keeps resources open. A failed
shutdown drain raises an error rather than disposing resources still in use;
the launcher may ultimately terminate an unresponsive process as before.

## CLI and source changes

- `toolframe check --format json` validates definitions, configuration, dependency
  order, source and schemas. Its `pluginValidation` is `definitions-only`:
  it does not run custom setup or prove external service readiness. Database
  validation connects read-only to PostgreSQL or uses a temporary SQLite copy;
  its exact validation level is reported separately.
- `toolframe invoke` starts configured plugins, calls the real MCP tool, then
  cleans plugins before returning. Plugin stdout is kept out of its JSON output.
- `toolframe serve` starts plugins before reporting readiness and closes them
  after MCP/Streamlit stop.

Plugin catalogs, domain models/contracts, installed packages, migrations, routes
and MCP schemas remain startup-loaded. The host prepares the domain namespace
before catalog imports, including with a custom `--domains-dir`. Streamlit reruns
reload UI modules while preserving live service and contract identities; model or
catalog edits require restart. This release does not dynamically run pip,
coordinate multiple workers, isolate plugins, or provide a marketplace.

`manager.register()` and `unregister()` update the trusted service graph at runtime.
They do not install packages, load new MCP tools/widgets, or discover/apply schemas.
Use the host's package/catalog/schema startup workflow when adding those features.

## Owned resources and local events

`ctx.scope()` returns a child `EffectScope`; `ctx.task(coroutine)` starts work owned
by the plugin lifetime. Tasks are cancelled and joined before resource cleanup.
Register cleanup immediately with `ctx.on_dispose`, `ctx.effect`, `enter_context`
or `enter_async_context`. Child resource scopes can close independently; they are
different from service resolution scopes. Cancellation must remain cooperative:
the drain timeout bounds leased calls, not arbitrary setup or cleanup callbacks.

`EventKey(name, payload_type)` identifies a typed local event. `ctx.on` owns a
handler and removes it during cleanup. `await manager.emit` waits for handlers
and their service leases. Event names belong to the manager, not to a tenant;
events are process-local and non-durable, with no replay for stopped handlers.
Keep audit records/outbox delivery in durable storage. The
[executable resource/event example](../examples/plugins/reactive_effects.py)
demonstrates these primitives without adding background jobs to the product.

## Attach a plugin's schema

`app/plugins.py` may export `SCHEMAS = {PLUGIN.id: schema}` alongside
`PLUGINS`. Each value is a `toolframe_migrations.Schema`, or the result of
`read_schema(Path(...))`. The named plugin must be installed in `PLUGINS`.
Toolframe automatically adds its database service dependency. Domain schemas in
`domains/<owner>/migrations/schema.json` are discovered without this mapping.
For a scoped definition, the ID is `scope/name`; use that ID for schema mappings,
domain bindings and management URLs (URL-encode it). This is service resolution,
not tenant isolation or a dynamically loaded domain.

Schema targets describe installed code, including a plugin configured with
`enabled=False`; disabling changes service availability, not the installed schema
requirements. Uninstall by removing the definition and its schema declaration,
then restarting. Existing data, ownership and archived history remain. Declare
revision dependencies separately from runtime `requires`. See
[schema authoring](plugin-migrations.md) and the
[executable lifecycle example](../examples/plugins/README.md).

## Update the embedded package

The designated package checkout is the source of truth. Edit and test it first, then:

```sh
python3 scripts/sync-fastapi-cordis.py --source ../fastapi-cordis-reactive
python3 scripts/sync-fastapi-cordis.py --source ../fastapi-cordis-reactive --check
cargo test --all-targets
cargo build --release
```

`core/fastapi_cordis/UPSTREAM.json` records the package version, dependencies,
source and wheel hashes. Sync uses `uv build` with a fixed build timestamp to
produce a reproducible wheel. The wheel is supplied to UV as a local source for
`fastapi-cordis==0.2.0`, so other plugins can declare a normal dependency on it
before it is published to PyPI. The source copy, wheel and MIT license are embedded
in the executable and included in its runtime cache hash. Regular builds work
without the sibling repo. Do not hand-edit the embedded copy.
