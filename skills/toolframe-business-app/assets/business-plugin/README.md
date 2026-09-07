# Stockroom: one complete business plugin

This small application records a delivery once and reports received stock totals.
Its human form and two MCP operations lease the same business service. It is a
copyable development example, not a full inventory/dispatch product.

## Start in a fresh directory

From a Toolframe folder with the current installed/exported skill, copy this
asset into a **new** app directory and put the new executable beside its `app/`
and `domains/` folders. For example, after exporting the skill:

```sh
./toolframe export-skill ./business-skill-new
mkdir stockroom-demo
cp -R business-skill-new/assets/business-plugin/. stockroom-demo/
cp ./toolframe stockroom-demo/toolframe
mkdir stockroom-demo/workspace
cd stockroom-demo
./toolframe check --format json
./toolframe invoke stockroom_receive_delivery --json '{"reference":"DEL-001","item":"Notebook","quantity":3}'
./toolframe invoke stockroom_receive_delivery --json '{"reference":"DEL-001","item":"Notebook","quantity":3}'
./toolframe invoke stockroom_stock_levels
./toolframe serve
```

The second delivery invocation returns `replayed: true`; the total remains three.
The default SQLite file is local to this new app. For disposable PostgreSQL
verification, configure `TOOLFRAME_DB_PROVIDER=postgresql` and
`TOOLFRAME_POSTGRES_DSN` before check/invoke/serve; changing targets does not copy
the SQLite deliveries. When adding this capability to an existing app, merge the
catalog, dependencies, dashboard/peer composition and handoff documents; preserve
its existing files rather than copying the example's app root over them.

## Follow one capability through the source

1. `domains/stockroom/model/contract.py` defines the `Stockroom` Protocol and
   `STOCKROOM` ServiceKey, exported by the domain's root `__init__.py`.
2. `model/service.py` owns validation, stock queries and the idempotent transaction
   for delivery receipt plus stock. Individual rules/entities are ordinary code.
3. `model/plugin.py` defines `stockroom.local`, provides that contract and requires
   the host `DATABASE` service. Setup checks schema readiness; it does not create
   tables or close the host database. Its validated setting is the maximum delivery
   quantity. It owns no additional resource, so it needs no artificial cleanup.
4. `app/plugins.py` registers this implementation and binds it to the automatically
   created `domain.stockroom` gate. The gate is not another authored plugin.
5. `migrations/schema.json` belongs to schema owner `stockroom`. Startup discovers
   and applies its selected SQLite/PostgreSQL variant before the service starts.
   There is no duplicate `SCHEMAS` registration for this local manifest.
6. `tools/` and `ui/stockroom.py` lease the same service and call its business methods.
   The linked peer guide describes only those two MCP operations.

This local plugin needs no extra dependency: FastAPI, fastapi-cordis, Pydantic and
the database SDK are already installed by Toolframe's managed environment. A real
third-party dependency goes in `app/pyproject.toml`, then a restart installs it.
An installed package can export a Plugin imported by this same catalog; making
each local capability a separate PyPI project is unnecessary.

## Check lifecycle and ownership

In the running app, receive another delivery through the human form and read its
total using MCP. In `/info`, Stop `stockroom.local`: `domain.stockroom` becomes
blocked and new MCP/widget work is unavailable. Enable the service: its enabled
consumers recover automatically and previous deliveries remain. Stop with dependents
also disables the domain's intent, so enable both explicitly afterward.

Try a quantity above the configured maximum: MCP reports a business error and the
form uses the core error card. Reuse an existing reference with changed contents:
the idempotency contract rejects it. Confirm neither attempt adds stock. After a
restart, the same original delivery reference still replays its receipt.

Cross-domain code imports `STOCKROOM` from `domains.stockroom`, declares it in a
retained consumer plugin's `requires`, and uses `ctx.require(STOCKROOM)` during
setup. Independent call-time consumers hold `use_plugin(STOCKROOM)` for the whole
operation. Keep their domain bindings connected; never import Stockroom's SQL/model
internals from another business domain. Public convenience functions must own
their lease internally.

Catalog/model/schema/package changes require restart. UI reruns reload UI modules
while retaining the live service and contract identities. Runtime registration
does not install packages, add MCP/widgets or run migrations. Events are local and
non-durable; use durable audit/outbox records where needed. Uninstalling the
capability removes its domain/catalog/peer entry on restart but retains data,
receipts, ownership and archived migration history.
