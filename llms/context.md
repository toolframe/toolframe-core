# APP_CONTEXT LAN Projection Template

Use this as the compact LAN-facing projection of the app's `APP_CONTEXT.md`. Replace each `[CUSTOMIZE: ...]` value with a concrete fact after inspecting the current source, migration, dashboard, and tool list. Keep unknown owner decisions explicit; do not turn placeholders into promises.

## App identity

- Application name: `Unnamed` — replace this with a short, business-specific name selected by the owner or proposed by the development agent.
- Framework: Lite MCP. Keep this as technical attribution rather than using it as the application name.
- Naming rationale: `[CUSTOMIZE: which business noun or outcome makes this app recognizable among other LAN apps?]`

## Purpose

`[CUSTOMIZE: What business outcome does this app make easier, and what proves a successful interaction?]`

## Actors

| Actor | Responsibility | Can see or change |
| --- | --- | --- |
| Human owner | `[CUSTOMIZE]` | `[CUSTOMIZE: dashboard views/actions]` |
| Peer or scheduled agent | `[CUSTOMIZE]` | `[CUSTOMIZE: published MCP tools/data]` |
| Other actor | `[CUSTOMIZE or None]` | `[CUSTOMIZE]` |

## Business loop

1. Trigger or intake: `[CUSTOMIZE: who/what starts an item]`.
2. Read: `[CUSTOMIZE: authoritative record, context, and eligibility]`.
3. Act: `[CUSTOMIZE: narrow tool or dashboard action and its guard]`.
4. Review: `[CUSTOMIZE: who reviews, which state/result is visible]`.
5. Completion: `[CUSTOMIZE: terminal evidence and owner follow-up]`.

## Vocabulary and rules

| Business term | Meaning in this app | State or invariant |
| --- | --- | --- |
| `[CUSTOMIZE]` | `[CUSTOMIZE]` | `[CUSTOMIZE]` |

`[CUSTOMIZE: If work is genuinely polled, claimed, leased, retried, or human-reviewed, link the app's queue/domain rules. Otherwise state that this is ordinary CRUD or another non-queue workflow.]`

## Interfaces

- Dashboard: `/` — `[CUSTOMIZE: human entry, review, and status]`.
- Built-in diagnostics: `/info` — use for runtime facts and safe diagnosis.
- Readiness: `/healthz` — server readiness only, not business success.
- MCP: same-origin `/mcp` — `[CUSTOMIZE: actual narrow business tools and purpose]`.
- Database and artifacts: `[CUSTOMIZE: data.db, app-prefixed tables, and managed artifact references]`.
- Scheduler boundary: external — `[CUSTOMIZE: trigger owner/cadence, or None]`.

## Boundaries

This is unauthenticated trusted-LAN infrastructure. `[CUSTOMIZE: trusted users, data sensitivity, and acceptable scale]`. It is not a public Internet service or an identity, authorization, tenant-isolation, or secret-protection boundary. Do not put secrets or high-stakes data here without an explicit owner decision and suitable dedicated architecture.

## Handoff and open decisions

- Last verified: `[CUSTOMIZE: date/time and source snapshot]`.
- Verification: `[CUSTOMIZE: checks, tool-list evidence, and representative workflow result]`.
- Restart after: migrations, MCP tools, dependencies, and other startup-loaded files; dashboard source may rerun on browser interaction but this is not MCP hot reload.
- Open owner decision: `[CUSTOMIZE: question, impact, and safe default]`.
- Next business change: `[CUSTOMIZE: one bounded next step]`.
