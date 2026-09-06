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

## Add a service plugin

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
enabled plugins depending on disabled plugins stop startup.

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

Open `/info` for plugin states, dependencies, in-flight counts and Enable/Stop
buttons. Stop dependents before their providers. The HTTP interface also supports
explicit cascades and configuration updates:

```sh
curl http://127.0.0.1:8000/api/plugins
curl -X POST http://127.0.0.1:8000/api/plugins/domain.entry_register/disable
curl -X POST http://127.0.0.1:8000/api/plugins/domain.entry_register/enable
curl -X PUT http://127.0.0.1:8000/api/plugins/formatter/config \
  -H 'Content-Type: application/json' -d '{"prefix":"Summary"}'
curl -X POST 'http://127.0.0.1:8000/api/plugins/formatter/disable?cascade=true'
```

These endpoints use Toolframe's existing trusted-LAN access and host/origin
checks. Responses omit configuration values and service objects. Changes apply
to the running process only. Put durable defaults in `app/plugins.py`; a restart
loads those defaults again. After a cascade, enable providers before dependents.

Configuration is validated before interrupting work. A valid update drains and
restarts the affected active dependency graph. If new setup fails, that graph
stays unavailable with the attempted configuration retained; fix it and enable
providers before dependents. Other plugins continue running. External side
effects are not rolled back.

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

Source files, installed packages, migrations, routes, and MCP schemas remain
startup-loaded. This release does not hot-reload Python modules, dynamically run
pip, coordinate multiple workers, isolate plugins, or provide a marketplace.

## Attach a plugin's schema

`app/plugins.py` may export `SCHEMAS = {"your.concrete_plugin": schema}` alongside
`PLUGINS`. Each value is a `toolframe_migrations.Schema`, or the result of
`read_schema(Path(...))`. The named plugin must be installed in `PLUGINS`.
Toolframe automatically adds its database service dependency. Domain schemas in
`domains/<owner>/migrations/schema.json` are discovered without this mapping.

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
