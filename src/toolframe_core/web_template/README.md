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

Source inputs live in `web-assets.toml` and `web_src/`; generated outputs are staged by `toolframe web prepare` before the wheel is built. Never put credentials in public assets. Keep private data out of htmx history snapshots and render user content as escaped data.

The example uses portable SQL for its notes table. It exposes ordinary business methods through the SDK. Web handlers may call only business methods declared by their owner or its requirements. A read-only password can view notes; a password with both read and write scopes can create and edit them.
