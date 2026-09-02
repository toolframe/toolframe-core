# Operations

## Lifecycle and restart

Start the packaged executable from the app root. Startup validates and applies pending migrations before the listener is ready, loads the dashboard and every published MCP tool once, and then serves the dashboard and same-origin `/mcp`. Migrations, tool code, tool dependencies, and other startup-loaded files require a complete stop and restart. Dashboard source can rerun on browser refresh or widget interaction; that is not MCP tool hot reload. There is no reload command, API, or UI.

The scheduler and durable worker loop are external. Lite MCP stores and exposes business state; it does not wake an agent, guarantee delivery, or coordinate workers by itself.

## Safe diagnostics

- `GET /healthz`: simple server readiness; it does not prove that a business tool, migration, or workflow works.
- `GET /info`: runtime, route, and diagnostic information for the current server.
- Dashboard `/`: human-visible data and review state.
- MCP `/mcp`: use the client registration flow and list tools; do not infer availability from this page.
- A coding agent with filesystem access may run `./lite-mcp check --format json` before a restart and inspect the app's local logs. Keep the exact command output, URL, and trace IDs with the report.

Do not use arbitrary SQL as a business diagnostic or repair shortcut. `execute_sql` is a trusted coding/repair reference that can mutate schema and data. Back up or preserve the database before consequential repair, and let a server-side coding agent inspect migrations, tools, and dashboard source.

## Errors and retryability

Retry only when the observed error is documented or structured as retryable, and first apply the relevant idempotency, version, or lease guard. Busy/locked storage and temporary I/O or upstream failures may be retryable; invalid arguments, missing tools, constraint conflicts, schema/startup failures, and unknown internal errors require correction or diagnosis. A timeout is not proof that a mutation did not commit: read authoritative state before repeating it.

If MCP transport, route, DNS, timeout, or server availability fails, pause the workflow before changing business state. Report the exact attempted URL and error, distinguish peer reachability from proof of process failure, and include one self-contained copyable issue with impact, observations, unknowns, safe owner checks, and maintainer next action.

## Security boundary

The service is unauthenticated trusted-LAN infrastructure. Reachable LAN clients may access the dashboard, call trusted tools, and reach the data those tools expose. Do not expose it to the public Internet or treat actor names as identity, authentication, authorization, tenant isolation, or secret protection. Keep the port, database, workspace, and logs private to the trusted network; logs may contain sensitive payload fragments and are not automatically redacted.
