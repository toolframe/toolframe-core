---
name: toolframe-plugin-design
version: "0.3.0"
description: Load when designing, creating, or extending a Toolframe business plugin, including its domain model, persistence, or dashboard and MCP interfaces. Excludes routine application operation, calling existing tools, standalone Streamlit apps, Codex plugins, and Toolframe runtime/provider internals.
---

# Toolframe plugin design

Turn the user's business outcome into an independently packaged Toolframe plugin.
This skill targets Toolframe core 0.3 and SDK 0.2. The attached references and
starter travel with the application; a Toolframe source checkout is optional.

## Establish the design

Identify the application home, existing plugin source (if any), and whether the
request is design-only or includes implementation. Preserve existing plugin IDs
and data ownership when extending a plugin.

Read [design.md](references/design.md) when establishing or changing business
behavior. Resolve the operations, invariants, data ownership and required user
surfaces before choosing tables or widgets. Finish this stage with a concise
design covering those decisions and any consequential unresolved questions.
For a design-only request, deliver that design and stop before creating code or
changing the application. An implementation request already authorizes ordinary
coding; continue without an extra design-approval ceremony.

## Implement the plugin

For implementation, read [plugin-api.md](references/plugin-api.md) before defining
services, storage or resources. Use public `toolframe_sdk` contracts. Business
state belongs in provider-managed storage; business services own its meaning.
Dashboard pages and MCP tools call those services.
Declare owned read/write scopes and each method's audience. Use page permissions
for navigation and controls; core enforces service permissions. Choose public
MCP/API publication explicitly: those entrances remain public even when Dashboard
requires a password. [plugin-api.md](references/plugin-api.md) defines this boundary.

For a **new** plugin, read [operations.md](references/operations.md), then run
`toolframe --home APP plugin new PLUGIN-ID --json`. Customize the generated source
at `APP/plugins/PLUGIN-ID/`; `--output PATH` selects an exact alternative directory.
The CLI renders the core-packaged [Notes example](assets/sample-plugin), including
consistent package, module and service identities. Generation alone leaves the
application composition unchanged. Adapt its schema and behavior to the design;
remove unneeded presentation resources, UI extras or storage declarations.
For an **existing** plugin, edit its source directly; do not regenerate it.
Generated sources belong to the application and survive binary/skill updates.
Finish implementation with a buildable wheel, explicit scopes and service schemas and
checks for the requested business rules.

## Validate in the application

When installing or testing code, read [operations.md](references/operations.md).
Distinguish package validation, temporary runtime validation and active health.
Existing-data migrations require a reviewed migration plan and an explicitly
authorized apply; a normal reload does not apply them. Report evidence for the
affected service and each requested presentation surface, retained data after
reload, and any checks not run. An installed package alone is not completion.
