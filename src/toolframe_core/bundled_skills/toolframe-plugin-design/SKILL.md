---
name: toolframe-plugin-design
version: "0.5.1"
description: Load when designing, creating, or modifying a Toolframe business plugin, including HTML Web apps, forms and lists, domain services, persistence, Streamlit Dashboard pages, or MCP tools. Excludes routine operation, calling existing tools, standalone HTML or Streamlit apps, Codex plugins, and Toolframe runtime/provider internals.
---

# Toolframe plugin design

Turn the user's business outcome into an independently packaged Toolframe plugin.
This skill targets Toolframe Core 0.8.0, SDK 0.4.1 and Web contract v1. The attached references and
basic template travel with the application; a Toolframe source checkout is optional.

## Establish the design

Identify the application home, existing plugin source (if any), and whether the
request is design-only or includes implementation. Honor the user's explicit file
and directory destinations. For a new complete application with no chosen home,
use the delivered project directory as its application home. Preserve an existing
application's home, plugin IDs and data ownership. For application setup, read
[home and startup](references/operations.md#application-home-and-startup) before
choosing a layout or helper scripts.

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
Presentation handlers and MCP tools call those services.
Declare owned read/write scopes and each method's audience. Use page permissions
for navigation and controls; core enforces service permissions. Choose each entrance explicitly. Confidential applications need protected browser
access with `failure_mode="closed"`, a separate protected MCP policy, and no sensitive
`public_api` methods or llms documents. Preserve an existing application's explicit
policy until an authorized change. Method scopes do not provide person/tenant or
row isolation. [plugin-api.md](references/plugin-api.md) defines these boundaries.

For an HTML **Web app**, read [web-authoring.md](references/web-authoring.md)
before selecting a starter, browser libraries or asset packaging. It covers the
Web contract, the recommended htmx/Tailwind recipe, and installed verification.
For new forms and lists without a requested framework, recommend Web with htmx
and Tailwind standalone. Keep an existing app's explicit framework choices.
Start new Web presentation code with FSD `app` and `pages/<slice>`; the reference
defines Python/ESM public APIs and where templates, assets and adapters belong.
Dashboard-only work explicitly selects `toolframe-streamlit-dashboard/notes`.
Web UI does not publish an MCP tool. When agents need to consume the business
operations, read the optional MCP recipe in [plugin-api.md](references/plugin-api.md)
and decide its access policy independently.

For a **new** plugin, read [operations.md](references/operations.md), then inspect
`toolframe --home APP template list` and `template show DISTRIBUTION/TEMPLATE --json`.
Choose `toolframe-core/basic` for a minimal provider-free service (the default),
`toolframe-web/notes` for Web, or `toolframe-streamlit-dashboard/notes` for Dashboard.
For an unavailable optional template, consult `template list --catalog` and supply
an available version or matching wheel with `--from`; catalog presence does not
prove publication. Read the selected wheel's parameter schema before generation.
Run `plugin new PLUGIN-ID --template DISTRIBUTION/TEMPLATE --json`, using the same
`--from` when needed. Customize the returned source path; `--output PATH` selects
an exact alternative. Generation prepares an isolated authoring environment but
does not add, enable or mount a runtime plugin. Install required providers as a
separate application step. Optional boilerplate belongs to its distribution;
this skill carries guidance, not a second copy of those templates.
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
For complete application delivery, verify the documented official Toolframe CLI
command from the selected home, including restart and retained data, as described
in [completion evidence](references/operations.md#evidence-for-completion).
