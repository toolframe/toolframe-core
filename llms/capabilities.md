# Capabilities

This page separates the fixed Lite MCP server contract from the editable business surface. The connected server's successful tool listing is the source of truth for the current catalog; this page must be customized when the app grows.

## Fixed server surfaces

- MCP: same-origin `/mcp`, unauthenticated trusted-LAN FastMCP Streamable HTTP.
- Dashboard: `/`, the human-facing Streamlit app.
- Diagnostics: `/info`.
- Readiness: `/healthz`; this is not a business-health or workflow-completion report.
- Scheduling: external; Lite MCP does not provide a scheduler or durable worker queue.

## Fresh scaffold

A fresh scaffold starts with the trusted `execute_sql` MCP tool and the `example_card` dashboard widget. `execute_sql` executes one SQLite statement and can read, write, alter schema, or run PRAGMA statements; it is intentionally a trusted coding/repair reference and can permanently change data. `example_card` demonstrates the dashboard API against example entries; it is not a production workflow or MCP tool.

Do not claim that the scaffold already supports a user's business workflow. A workflow is available only when its narrow business tools are present in the connected tool list and its state rules are documented below.

## CUSTOMIZE: business capability register

Replace the examples below with the app's actual business contract. Keep one entry per published narrow tool and remove stale entries. Every entry should name its read or mutation role, accepted inputs, bounded result, state preconditions, guard or idempotency rule, retryability, and human-review consequence.

| Tool | Role | Inputs and result | Guard, retry, and review |
| --- | --- | --- | --- |
| `[CUSTOMIZE: actual_tool_name]` | `[read / mutation]` | `[schema and bounded result]` | `[precondition, version/idempotency/lease guard, retryability, review]` |

### CUSTOMIZE: dashboard and artifacts

- Human dashboard path: `/` — `[CUSTOMIZE: pages, fields, actions, and review evidence]`.
- Managed artifacts: `[CUSTOMIZE: whether workspace/artifacts is used and how references are returned]`.
- Business data source: `[CUSTOMIZE: app-prefixed tables and authoritative fields]`.
- Current catalog verified at: `[CUSTOMIZE: date, server/source snapshot, and tool-list evidence]`.

Do not list an implementation idea as an available capability. If a required tool is absent, follow [Workflow](/llms/workflow.md) and request server-side implementation with filesystem access.
