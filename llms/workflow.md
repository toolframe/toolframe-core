# Workflow

Build or execute the smallest workflow that satisfies the user's request. Start with the business outcome and the app's documented state rules; use the published Business RPC surface rather than treating tools as database resources. Do not make a generic queue model fit an ordinary register or lookup request.

## Default sequence

1. **Trigger** — A human request, external scheduler, or peer-agent invocation starts the work. Scheduling is outside Toolframe; record the trigger and correlation key.
2. **Business query** — List actual tools, read the relevant context, and call the bounded query that answers the workflow question: eligibility, current state, context, evidence, or review need.
3. **Business command** — Call the command whose named intent matches the requested outcome. Honor its preconditions, version/fence/lease guard, idempotency key, and returned next action. Let the command own its guarded outcome and local transaction; do not assemble table CRUD or generic status/field writes. Use `execute_sql` only for a data fallback explicitly allowed by this app's capability register; never substitute it for a missing business operation or schema change.
4. **Review** — Make the result and next decision visible through the documented dashboard or review tool. Preserve the returned state, trace ID, artifact reference, and any owner decision needed.
5. **Completion** — Claim completion only when the tool result and authoritative read show the intended terminal or review state. Report what changed, what remains for a human, and the evidence used.

## Queue rules

Use a queue/job lifecycle only when the request or app actually polls, claims, leases, retries, or waits for human review. For that case, document the item state, atomic claim, lease/fence, retry budget, and review transition in [Application context](/llms/context.md) and [Capabilities](/llms/capabilities.md). Do not invent jobs, attempts, or worker ownership for a non-queue workflow.

## Errors and unavailable MCP

Treat structured tool errors according to their code and `retryable` value. A retryable error does not make repeating a mutation safe; inspect state and use the documented idempotency or guard before retrying. A validation, conflict, permission, missing-tool, or other non-retryable error requires correction or escalation.

If `/mcp` cannot be reached or the client cannot list tools, pause before business reads or writes. Say that the workflow is paused, state which operations did not happen, preserve the exact URL and client error, and provide the copyable MCP-unavailable issue described in [Operations](/llms/operations.md). Do not mark the business item failed, done, or blocked merely because peer connectivity is unknown.

## CUSTOMIZE: executable workflow

Replace this section with the app's actual mapping from user intent to tools and states:

- Trigger: `[CUSTOMIZE: event/request and external owner]`
- Business queries: `[CUSTOMIZE: exact tool names, question answered, bounded evidence]`
- Business commands: `[CUSTOMIZE: exact tool names, outcome, preconditions, idempotency/version/lease keys]`
- Review: `[CUSTOMIZE: dashboard/tool, reviewer, and visible result]`
- Completion: `[CUSTOMIZE: terminal state, audit/result/artifact evidence, and next owner action]`
