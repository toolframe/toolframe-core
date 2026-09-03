# Capabilities

This page separates the fixed Lite MCP server contract from the editable business surface. The connected server's successful tool listing is the source of truth for the current catalog; this page must be customized when the app grows.

## Fixed server surfaces

- MCP: same-origin `/mcp`, unauthenticated trusted-LAN FastMCP Streamable HTTP.
- Dashboard: `/`, the human-facing Streamlit app.
- Diagnostics: `/info`.
- Readiness: `/healthz`; this is not a business-health or workflow-completion report.
- Scheduling: external; Lite MCP does not provide a scheduler or durable worker queue.

## Fresh scaffold

A fresh scaffold starts with the trusted `execute_sql` MCP tool and the `example_card` dashboard widget. `execute_sql` is a bounded fallback for parameterized data reads and DML through the shared runtime adapter. It cannot alter schema, run PRAGMA/maintenance SQL, attach databases, or mutate runtime-owned tables. Writes can still permanently change app data. `example_card` demonstrates the dashboard API against example entries; it is not a production workflow or MCP tool.

Do not claim that the scaffold already supports a user's business workflow. A workflow is available only when its Business RPC commands and queries are present in the connected tool list and its state rules are documented below.

Peer workflow agents should use `execute_sql` only when this app-specific register explicitly documents the fallback query or mutation. A coding agent with filesystem access owns schema changes through the next migration; no MCP tool is a schema-administration path.

## CUSTOMIZE: Business RPC capability register

Replace the example below with the app's smallest intent-shaped catalog and remove stale entries. A business query answers one bounded workflow question. A business command completes one guarded outcome or legal state transition and may hide several database reads/writes, events, and receipts. Do not mirror tables with create/read/update/delete tools or expose generic patch, status, filter, sort, column, or SQL controls.

| Tool | Kind | Caller intent and outcome | Business inputs and result | Guard, retry, and review |
| --- | --- | --- | --- | --- |
| `[CUSTOMIZE: intent-shaped tool name]` | `[business query / business command]` | `[question answered or transition completed]` | `[domain inputs; bounded state/evidence/version/next action]` | `[precondition, version/idempotency/lease guard, retryability, review]` |

Every entry must trace to a real user or agent intent and an observable business outcome. One command owns its complete guarded outcome and any required local transaction; a caller must not need to assemble public storage primitives to achieve it.

### CUSTOMIZE: dashboard and artifacts

- Human dashboard path: `/` — `[CUSTOMIZE: pages, fields, actions, and review evidence]`.
- Managed artifacts: `[CUSTOMIZE: whether workspace/artifacts is used and how references are returned]`.
- Business data source: `[CUSTOMIZE: app-prefixed tables and authoritative fields]`.
- Current catalog verified at: `[CUSTOMIZE: date, server/source snapshot, and tool-list evidence]`.

Do not list an implementation idea as an available capability. If a required business query or command is absent, follow [Workflow](/llms/workflow.md) and request server-side implementation with filesystem access instead of synthesizing it from CRUD or `execute_sql`.
