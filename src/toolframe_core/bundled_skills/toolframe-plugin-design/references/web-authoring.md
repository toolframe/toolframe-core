# HTML Web applications

Use this reference for a Toolframe business plugin whose presentation is a full
HTML document, forms, JSON or fragments. Core 0.8 and SDK Web contract v1 provide
this entrance through the separately installed `toolframe-web` provider. The
local SDK build is `0.4.0+web.1`; install matching wheels from a supplied wheelhouse.
It is compatible with existing plugins that require SDK's public version 0.4.0.

## Choose the presentation

Recommend htmx plus Tailwind for new forms and lists. Add other browser libraries
only when the requested interaction benefits from them. These are authoring
choices: ordinary CSS, custom JavaScript, Alpine's CSP build or a prebuilt client
application can use the same SDK contract. Each app owns its document and library
versions. Preserve existing choices when extending a plugin.

Generate new source with `toolframe --home APP plugin new NAME --presentation web`.
The included Notes example has managed SQL, business read/write scopes,
idempotent creation, row revisions, complete pages, fragments, JSON, and a dynamic
ESM import. Edit its business services and invariants first. Existing plugin source
is edited directly. A Web resource uses `kind="web.app"`, contract `1`, a stable
resource ID, `module:factory`, import package and sealed asset manifest path.
The factory returns SDK `WebApp` containing explicit `WebRoute` handlers.

## Organize the presentation with FSD

Start with the page that owns the workflow. The generated source uses:

```text
src/package/
  contracts.py                 # public service schemas and identifiers
  plugin.py, service.py         # business definition and durable behavior
  web/
    app/                       # composition, routes, entrypoint, global styles
      __init__.py, index.js, styles/input.css
    pages/notes/               # one independently owned page slice
      __init__.py, index.js     # Python and ESM public APIs
      ui/                      # SSR handlers, templates and browser interactions
      model/                   # form normalization and local view state
      api/                     # ctx.call adapter to public business contracts
```

`app` imports the page's public `__init__.py` or `index.js`; code outside a slice
does not reach into its internal segments. Imports within a slice may connect its
own UI, model and API. Different page slices do not import one another. Add
`shared` only for infrastructure/UI actually reused across pages. Add `features`
or `entities` only for a clear responsibility reused across pages, maintaining the
downward order `app → pages → features → entities → shared`. Do not create empty
layers, `widgets` or `processes` to fill out a diagram.

Keep templates, JS, CSS and page-specific assets beside their owning slice.
Global styles belong to `app`; genuinely shared UI belongs to `shared`.
The manifest can read these paths directly and preserves declared output paths
for relative/dynamic imports and CSS URLs. `web/assets.json` and `web/public` are
prepare staging/wheel outputs, not source layers. This Python SSR/native ESM
mapping needs no TypeScript build, Node/npm or Steiger installation. Existing
plugins keep their framework and structure unless a restructure was requested;
updating the template or installed skill never regenerates their source.

Handlers receive `WebRequest` and request-scoped `WebContext`. Use `await ctx.call`
to invoke declared business services; Core retains the visitor's identity and
checks resource, route and method scopes. GET/HEAD handlers can call only
`read_only` business methods. Keep domain mutations, idempotency and row ownership
in those services. Web code runs in a shared presentation cohort, separately from
the owner service and Streamlit; do not import business implementations into it.
The presentation's `model` and `api` names do not transfer business ownership:
transactions, idempotency, row revisions and person/tenant authorization remain
in the service. Pure public schemas/identifiers may be shared; service classes,
SQL connections and worker state may not. The starter's notes are shared among
authorized callers. It does not implement private per-person rows.

The starter's service methods declare descriptions, required fields, closed
object schemas, string lengths, positive integer revisions and bounded outputs.
Keep that contract complete when adapting it. Optional MCP tools target those
same methods and schemas; Web UI alone does not make them callable by an agent.
Use the private MCP recipe in [plugin-api.md](plugin-api.md) when requested.

## Prepare the wheel

Run `toolframe --home APP web prepare SOURCE` and install the printed wheel path.
Core captures source, invokes the Web preparation CLI and builds the wheel from
that staging tree. The author's package is not filled with generated assets.
A source install performs the same staging. `uv build` alone does not prepare a
source recipe. Installed wheels use sealed bytes and need no compiler or CDN.

`web-assets.toml` lists every browser-ready local or pinned HTTPS file, SHA-256,
MIME-compatible extension, dependency and license file. Preserve relative paths
for dynamic imports, CSS URLs, fonts, workers and WASM; include the entire graph.
The manifest is an explicit allowlist, not a JavaScript dependency resolver.
The recommended Tailwind recipe uses its pinned standalone executable, without
installing Node/npm. Scan only the listed sources and use complete utility class
names. Prebuilt CSS skips the compiler. `web prepare --offline` requires cached
remote inputs and compiler; a missing input fails without changing the active app.

## Mount, protect and verify

Install a SQL provider and `web`, select schema version 2, and explicitly mount
`web.NAME.RESOURCE`. Root can belong to Web or `provider.dashboard`; installing a
provider does not select root. Use `ctx.url`, `ctx.asset` and `ctx.links` so forms,
assets and navigation follow the selected mount. Choose a `landing_target` when
root is only an entrance list. Core management, OAuth, MCP and llms paths stay fixed.

Return complete documents for navigation and fragments only when requested.
Use autoescaped `toolframe_web.render`, `asset_tags(ctx)` and
`ctx.form_fields()|safe`; the last helper contains Core-issued proofs, not user
HTML. Unsafe JSON requests send `X-CSRF-Token` and `X-Web-Revision`. After a stale
revision, ask for a full reload and check the domain outcome before retrying.
Keep 401/403/409/422 handling explicit; a fragment must never replace its content
with a login document. Disable sensitive htmx history snapshots. Insert user text
as escaped data, never executable markup or framework attributes.

CSP is strict by default. Use browser-ready CSP-compatible libraries; enable only
the app capabilities actually required. Apps share a trusted origin, so this is
not an untrusted-code sandbox. Generic ASGI mounts, WebSockets, SSE and streaming
responses are outside Web contract v1.
For classic or module Workers, declare `worker-src: ("'self'",)` and all script,
fetch and WASM dependencies in the allowlist. Worker script responses use the
current app's script/connect capabilities; WASM compilation needs an explicit
`'wasm-unsafe-eval'` script capability. Public scripts revalidate a policy-aware
ETag, while HTML/SVG asset responses retain their restrictive sandbox policy.

Finish with package checks and installed evidence: no-Node prepare/install,
public Core URLs, anonymous/reader/writer requests, full forms and fragments,
CSRF/revision denial, app-local assets, reload failure preserving the active app,
retained notes, and the requested Web/Dashboard mount configuration. Exercise
service permissions through the browser entrance as well as the CLI. Report
external authentication and platform combinations not run separately.
