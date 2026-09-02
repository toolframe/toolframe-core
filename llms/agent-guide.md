# LAN Agent Guide

This page is for an agent that received an origin URL ending in `/llms.txt` and a user request. It describes the connection and discovery contract; the running server's MCP tool list remains authoritative.

## Connect and prove the server

1. Derive the origin from the URL that supplied this page. Register the same origin with the path `/mcp` in the agent client's own MCP registration flow. Do not replace the host or port with a guessed value.
2. Use a clear server name chosen by the client or user (for example, `lite-mcp`) and transport `Streamable HTTP`. If the client cannot register an MCP server, report the required name, exact `/mcp` URL, transport, and the client-side blocker. Do not invent a client-specific command, config format, or installation result.
3. After registration, list tools through the client. Only a successful tool listing proves that the agent is connected. A page fetch, `/healthz` response, or registration record alone is not proof of MCP connectivity.
4. If connection fails, use the safe diagnostics in [Operations](/llms/operations.md). Keep the user's business workflow paused and report the exact URL, observed error, and unknowns; do not claim that the server is stopped based only on peer reachability.

## Read only what the request needs

After receiving a user request, read [Application context](/llms/context.md), then [Capabilities](/llms/capabilities.md), then [Workflow](/llms/workflow.md). Read [Operations](/llms/operations.md) when a restart, error, retry, or security boundary matters. These pages are intentionally split so an agent can fetch only the needed context.

Use the actual narrow business tools returned by the connected server. Check each tool's published input schema, current-state preconditions, result shape, idempotency or version guard, and retryability before calling it. Never infer a business tool from a filename, dashboard label, example, or this template.

`execute_sql` is a trusted coding and repair reference in a fresh scaffold, not a production business API. Do not use it to bypass a missing business capability or to invent a workflow. `example_card` is a dashboard demonstration, not an agent tool or a business contract.

## When the request needs new server capability

If the current tool list does not cover the user's requested business action, say which capability is missing. A server-side coding agent with filesystem access must add and verify the narrow migration, MCP tool, dashboard behavior, and app-local documentation; an online peer agent cannot create that capability through an arbitrary SQL call. Treat an existing client workflow and server-side app development as different tasks.

## Handoff result

Report the registered name, exact `/mcp` URL, transport, successful tool-list evidence, tools used, business state before and after, review needed, and any trace or artifact reference. If the workflow is paused, state that no business-state change is known and include the complete copyable issue required by the operations contract.
