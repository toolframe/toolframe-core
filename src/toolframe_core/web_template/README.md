# Notes Web plugin

This plugin stores notes through Toolframe SQL and presents them as complete HTML pages or htmx fragments. Updates use row revisions; create requests carry an idempotency key.

From the application directory:

```sh
toolframe web prepare ./plugins/web-notes
toolframe plugin add pypi:toolframe-web==0.1.0 --enable
# Install the wheel path printed by prepare:
toolframe plugin add wheel:./plugins/web-notes/build/web/toolframe_web_notes-0.1.0-py3-none-any.whl --enable
```

The application also needs a SQL provider. Set `schema_version = 2` and add an explicit mount to `toolframe.toml`, then run `toolframe apply`:

```toml
[gateway]
landing_target = "web.web-notes.main"

[gateway.mounts]
"web.web-notes.main" = { prefix = "/" }
# Add this only when the Dashboard provider is installed:
"provider.dashboard" = { prefix = "/console" }
```

The recommended recipe uses htmx 2.0.11 and Tailwind 4.3.3 standalone. Preparation downloads their pinned artifacts once; installed wheels serve local files without Node/npm or compilation. For plain CSS, remove `[tailwind]` and declare a local CSS file with `path = "app.css"` in `[[files]]`. Replace or remove the htmx script and module entries to use different browser libraries. Templates and browser assets belong to this app's full document.

Source inputs live in `web-assets.toml` and the presentation's owning FSD slices:

```text
src/toolframe_web_notes/
  contracts.py                # explicit service schemas and identifiers
  plugin.py, service.py        # domain ownership, transactions and idempotency
  web/app/                    # entrypoint, routes, global styles
  web/pages/notes/             # page public APIs: __init__.py and index.js
    ui/                       # handlers, templates and browser interactions
    model/                    # form normalization and character counter
    api/                      # public ctx.call adapter
```

The app imports each page through its public API. Keep same-layer page slices
independent; add lower `features`, `entities` or `shared` layers only when there
is actual reuse and a clear responsibility. Templates and page-specific assets
stay in their slice. No Node/npm, TypeScript build or FSD tooling is required.
Prepared `web/assets.json` and `web/public` are staging/wheel output rather than
source layers. The allowlist preserves relative ESM and CSS paths at any mount.
Never put credentials in public assets. Keep private data out of htmx history
snapshots and render user content as escaped data.

The example uses portable SQL for its notes table. Its described, bounded
list/create/update contracts are shared by all callers; service code owns
transactions, idempotency and row revisions. Web handlers use `ctx.call` and
cannot replace these rules with front-end state. A read-only password can view
notes; a password with both read and write scopes can create and edit them.
Notes are shared among authorized users, with no per-person/tenant isolation.
Set protected access with closed failure policy for confidential applications.

The starter publishes neither MCP tools nor public HTTP methods. To make these
operations available to agents, follow the optional MCP resource and independent
access-policy recipe in the application's installed
`.agents/skills/toolframe-plugin-design/references/plugin-api.md`. Keep sensitive
methods `public_api=False`. Existing plugins are edited directly; binary or skill
updates do not regenerate or overwrite them.
