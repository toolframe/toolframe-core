# Public plugin contract

Target: `toolframe-sdk==0.3.0`, Core 0.6.1, Python 3.14. Use this packaged reference
and the starter when the source workspace is unavailable. If the installed SDK
version differs, inspect its public definitions before adapting these examples.

## Package and service

One wheel provides one plugin, with one instance per application. A business
plugin depends on `toolframe-sdk`, not core or provider implementation modules.
The entry-point group is `toolframe.plugins`; its name and `PluginDefinition.id`
should be the same stable, lowercase hyphenated ID.

```toml
[project.entry-points."toolframe.plugins"]
sample-notes = "toolframe_sample_notes.plugin:plugin"
```

`plugin` is a `PluginDefinition`, not a factory function. It declares:

- `factory="package.module:Class"`: a subclass of `toolframe_sdk.Plugin`.
- `services={name: ServiceSpec(version=1, methods={...})}`: named business APIs.
- `MethodSpec(audience="business", required_scopes=("sample-notes.read",), input_schema=..., output_schema=..., read_only=...)`: JSON schemas
  for each method. Use bounded inputs and explicit object properties. JSON Schema
  defaults are documentation; implement actual Python defaults too.
- `requires={alias: Requirement(service=name, version=1, optional=False)}`:
  consumed services. In the worker, call
  `await self.context.call(alias, method, params)`.
- `config_schema`: accepted configuration; read values from `self.context.config`.

The host calls `await plugin.invoke(service, method, params)`. Dispatch only
declared methods and return JSON values matching the output schema. Signal
business failures with `PluginError(code, message, details)`. Use stable error
codes that pages and tools can interpret; avoid leaking credentials in details.

Keep module import and definitions free of side effects. `activate()` prepares
runtime resources, `health()` reports readiness, and `dispose()` releases owned
resources and awaits `super().dispose()`. Use `self.context.task(factory)` for
owned asynchronous work; account for cancellation. Each plugin runs in a separate
process. Cross-plugin Python objects and imports are not the service boundary.

## Authorization and publication

Every definition requires `authorization=AuthorizationSpec(scopes={...})`, even
infrastructure plugins with an empty catalog. Legacy definitions fail validation;
no whole-plugin fallback permission is invented. Each scope ID starts with the
owning plugin ID, for example `sample-notes.read` and `sample-notes.write`:

```python
authorization = AuthorizationSpec(
    scopes={
        "sample-notes.read": ScopeSpec(title="Read notes"),
        "sample-notes.write": ScopeSpec(title="Capture notes"),
    }
)
```

Business methods declare `audience="business"` and `required_scopes`. Internal
dependency services declare `audience="internal"` and have no visitor scopes;
they cannot be invoked directly by Dashboard/API clients. Managed SQL uses this
internal audience and the caller plugin's storage ownership. Do not require the
visitor to hold storage administration permission merely to submit a form.

Dashboard pages declare `Resource.required_scopes`. `context.can("sample-notes.write")`
can disable editing controls. The host-issued context is supplied by the provider;
page code does not construct principals or handle browser passwords. Nested
service calls keep the admitted caller and check the callee's declared scopes.

`read_only=True` describes mutation behavior; it does not make a method public.
Use `public_api=True` only for deliberately public business methods, served at
`POST /api/PLUGIN/SERVICE/METHOD`. This HTTP route and llms documents are public
independently of Dashboard passwords. Leave sensitive methods `public_api=False`
and keep private content out of published documents.

| Entrance | Required decision |
| --- | --- |
| Dashboard and `/_toolframe/v1/services/...` | Set `access.mode="protected"` and `access.failure_mode="closed"` for confidential data; enforce method scopes. |
| `/api/...` | Only explicitly public business methods are reachable; Dashboard/MCP credentials do not protect them. |
| `/mcp` | Independent `mcp_access` policy; public by default, or a healthy MCP auth provider and scoped MCP credentials in protected mode. |
| llms documents | Public; publish only intentionally shareable content. |
| Local control / management | Direct loopback control and the management token are operator recovery paths; remote system calls require system authority. |

An MCP tool resource publishes a business method to the MCP catalog, whose policy
is independent of Dashboard login. For confidential tools, install/enable a
compatible `toolframe-mcp-auth` provider and set:

```toml
[access]
mode = "protected"
failure_mode = "closed"

[mcp_access]
mode = "protected"
auth_provider = "mcp-auth"
```

Use business-scoped MCP keys or configured delegated user grants; Dashboard
passwords and management tokens are not MCP keys. Protected MCP stays unavailable
if its verifier is absent or fails, independently of Dashboard fallback. New Core
applications write `failure_mode="closed"`; older files omitting it retain legacy
public fallback. Review existing choices before changing them. The access-password
page shows entrance states; its failure-policy editor requires Core 0.6.1.

Scopes authorize methods, not individual rows. Password labels identify grants,
not people. Per-person or tenant data needs a trusted identity and explicit
business-layer row filtering. Never use a caller-supplied label as identity.
System/internal methods cannot be published as public APIs or MCP tools. The
starter deliberately publishes a public read-only query; remove that flag when
adapting it for private data.

Page `service.call(..., activity=False)` is for background polling, so it does not
extend a session's idle deadline or last-use timestamp. Interactive calls default
to activity. Every new call rechecks current access; already admitted call trees
retain their snapshot if a password is subsequently revoked. Plugin workers are
trusted application code, not a sandbox for untrusted extensions.

## Managed SQLite

Declare `requires={"storage": Requirement(service="toolframe.storage.sql")}`
and `store=StoreSchema(revision="v1", migrations=(Migration(...),))` when needed.
The provider allocates a database for the stable plugin ID. It retains data when
a plugin is disabled, removed or reloaded. Core's own SQLite holds management
state and is not business storage.

```python
storage = self.context.storage()  # requires alias "storage"
async with storage.transaction() as tx:
    result = await tx.execute("INSERT INTO notes(body) VALUES (?)", (body,))
    rows = await tx.query(
        "SELECT id, body FROM notes WHERE id = ?", (result["lastrowid"],)
    )
```

`query()` returns a list of row dictionaries. `execute()` returns `rowcount` and
`lastrowid`; use a transaction plus a query to read a mutation's result, rather
than SQL `RETURNING`. Bind values as parameters. Put DDL and indexes in migrations.
The provider manages connections, transaction control and file paths; business
code does not use `sqlite3.connect`, `ATTACH`, `PRAGMA` or raw `BEGIN`/`COMMIT`.
Nested and cross-owner transactions are unsupported. Bound query results and
paginate; do not fetch an entire unbounded history over RPC.

Migrations form a linear history: the first has `parent=None`, each later one
names the preceding revision, and `StoreSchema.revision` is the final revision.
Read SQL via `importlib.resources.files` and ship it inside the wheel. Applied
revision names and SQL checksums are immutable. New empty stores initialize
automatically; existing-store changes use the explicit migration operation.

## Optional resources

Publish resources only for requested surfaces. Missing presentation providers
leave the business service usable. A required service such as storage still
blocks activation when absent.

| Kind | `Resource.data` |
| --- | --- |
| `dashboard.page` | `entrypoint="package.page:render"`, optional integer `order`. |
| `mcp.tool` | `service`, `method`, `description`, `input_schema`, `output_schema`. Keep the schemas consistent with the target method. |
| `llms.document` | `package` and relative `path` to packaged Markdown. |

Use a stable `Resource.id` unique within its kind. Resources belong to their
plugin generation and are withdrawn when it stops. MCP names are prefixed with
the plugin ID, for example `sample-notes__list_notes`. Set a query's service
`MethodSpec.read_only=True`; write methods remain explicit mutations. The first
MCP provider supports tools, not resources or prompts.

Dashboard pages are ordinary synchronous `render(context: PageContext)` functions:

```python
def render(context):
    import streamlit as st

    service = context.services.require("sample-notes.notes", version=1)
    result = service.call("list_notes", {"limit": 20})
    st.dataframe(result["notes"])
```

Put Streamlit in the wheel's `ui` extra and import it in the page. The host resolves
page contributors together in the Dashboard environment; the business worker
does not need Streamlit. Page dependencies must coexist. Namespace widget keys
by plugin ID. Page code uses the public service facade and cannot open worker
database files. Reload may reset all Dashboard sessions, so retain durable data
through business services and surface stale-version conflicts to the user.

The llms provider publishes an index and the specifically declared documents.
Write those documents for application users and agents invoking its tools. Keep
private design notes, credentials and the authoring skill outside those assets.
