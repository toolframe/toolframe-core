# MCP unavailable workflow response

Use this response contract when a scheduled workflow or peer agent cannot reach
the configured app's `/mcp` endpoint. Replace every bracketed field with an
observed value or `Unknown`; never guess. Use the business app name from its
context or `/llms.txt`, not Lite MCP's framework name. Keep the issue
self-contained so the owner can copy it without the surrounding chat.

## Warning shown before the issue

> ⚠️ **[app name] MCP unavailable — workflow paused.** I could not reach the MCP
> endpoint, so I did not query pending work or write progress/results. No
> business-status change by this attempt is known. This is currently an
> integration-availability issue; the copyable report below contains the
> observed failure and recovery checks.

If a request reached MCP and returned a structured business/tool error, report
that exact error instead; this template is for transport, route, or server
availability failures before the intended business action completed.

## Copyable issue

Render the following as one fenced `markdown` block in the workflow response:

````markdown
# [app name] MCP unavailable: [workflow name]

## Summary

The workflow could not access the configured [app name] MCP endpoint. It
paused before reading or changing a business item.

- Observed at: [ISO-8601 date/time and timezone]
- Workflow or scheduled trigger: [name or Unknown]
- MCP URL attempted: [exact URL]
- Dashboard/base URL: [URL or Unknown]
- Calling client/agent: [name and version or Unknown]
- Caller host/source IP: [value or Unknown]
- Exact symptom: [connection refused, timeout, DNS error, HTTP status/body, or exact client error]
- Reachability known so far: [host check / peer check / Unknown]
- Retryable now: [yes / no / unknown] — [reason and prerequisite]

## Workflow impact

- Intended action: [for example, query pending work]
- Confirmed completed actions: [none or exact list]
- Confirmed incomplete actions: [exact list]
- Business data or status changed: [No known change, or exact evidence]
- Last confirmed business state: [value and observation time, or Unknown]
- Next scheduled attempt: [time/cadence or Unknown]

## Safe owner checks

1. On the Lite MCP host, confirm the `lite-mcp` terminal is still running and
   shows a ready message rather than a startup or shutdown error.
2. On that host, open the current printed Dashboard URL and `/healthz` URL.
3. Compare the workflow URL with the **current** printed LAN address plus
   `/mcp`; do not rely on an address copied from an older run.
4. Confirm the host and peer remain on the same intended LAN and that the host
   firewall permits the printed port.

## Maintainer diagnostics

- Run `./lite-mcp check --format json` in the app folder.
- Capture complete startup/shutdown terminal output.
- If logs exist, preserve the matching `.lite-mcp/logs/audit-YYYY-MM-DD.jsonl`
  and `.lite-mcp/logs/mcp-YYYY-MM-DD.jsonl` files as sensitive data.
- Test `/healthz` on the host first, then from the peer, and record the exact
  URL, status, and error at each hop.
- Verify the workflow uses Streamable HTTP at the exact `/mcp` route.

## Evidence attached

- Check report: [path/paste reference or Not collected]
- Terminal output: [path/paste reference or Not collected]
- Relevant trace/request ID: [value or Unknown]
- Relevant log files: [paths or Not collected]

## Recovery acceptance

- The app reaches ready state without a fatal startup error.
- `/healthz` succeeds on the host and intended peer.
- The peer lists MCP tools at the current LAN `/mcp` URL.
- A safe read call succeeds before the scheduled workflow resumes mutations.
````

Do not create an external issue automatically unless the user separately asks
for that external write. Return the warning and copyable block even when some
fields remain `Unknown`.
