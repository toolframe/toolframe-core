# App context

> Keep this file in the same folder as `lite-mcp`. Write for the next human or
> coding agent. Replace every bracketed prompt with a concrete fact; delete
> prompts that do not apply.

## Purpose

What business outcome does this local app make easier? What should be true
after a successful human or agent interaction?

## Actors

| Actor | Plain-language responsibility | What they can see or change |
| --- | --- | --- |
| Human owner | [e.g. reviews work and resolves exceptions] | [dashboard actions] |
| Scheduled/peer agent | [e.g. performs the next approved item] | [MCP tools and data] |
| Other actor | [business role] | [scope] |

## Business loop

1. [How an item enters the app.]
2. [How a human or agent finds the next thing.]
3. [What work, decision, or write happens.]
4. [How progress, result, and review are recorded.]
5. [How the owner knows what needs attention next.]

## Vocabulary

| Word used by the business | Meaning in this app | Avoid calling it |
| --- | --- | --- |
| [term] | [short, concrete definition] | [ambiguous synonym] |

If work is polled, claimed, or leased by a scheduled or peer agent, link the
chosen queue-lifecycle rules here: this app's `DOMAIN_MODEL.md` and the
installed `lite-mcp-business-app` skill's `references/job-domain-model.md`.
If the skill is bundled in this app folder, record its actual relative path;
do not assume one when it was installed separately. Ordinary CRUD results or
approval alone do not require that model. Do not invent a second meaning for a
canonical term.

## Interfaces

- Dashboard: `/` — [what a human sees first and what can be submitted].
- Built-in information: `/info` — [when to use it].
- MCP: `/mcp` — [trusted peer-agent tools and their business purpose].
- Database: `data.db` — [business data owned by this app].
- Scheduler boundary: [external trigger, cadence, or “none”].

## Boundaries

This app is a trusted, LAN-only, unauthenticated local service. It is suitable
for [trusted users and scale]. It is not suitable for [public access,
authentication/permissions, untrusted code, high availability, or any other
known escalation]. Never put secrets or high-stakes data here unless the owner
has consciously accepted the local exposure.

## Operating instructions

```sh
./lite-mcp check --format json
./lite-mcp
```

- Dashboard URL: [copy the URL printed for this run].
- MCP URL for this host: [copy the loopback URL printed for this run].
- MCP URL for a peer: [use the tested printed LAN host/port with `/mcp`].
- Restart required after: [migrations, MCP tools, dependencies, and other
  startup-loaded files].
- Dashboard refresh behavior: [browser refresh/rerun expectation].
- Diagnosis: [first check command and the relevant log/console location].
- MCP unavailable contract: [workflow pauses without changing business state;
  response uses the installed skill's
  `assets/MCP_UNAVAILABLE_ISSUE.template.md`; name the owner/next scheduler
  action].

## Source map

| Business capability | Source of truth | Notes for the next agent |
| --- | --- | --- |
| Schema | `migrations/` | [latest migration and immutable-history note] |
| MCP tools | `tools/` | [public tool names and private helper modules] |
| Dashboard | `dashboard/app.py`, `dashboard/widgets/` | [entry point and widget list] |
| Large results/files | `workspace/artifacts/` | [artifact naming and retrieval convention] |

## Decisions and open issues

### Decisions

- [Decision, date, and business reason.]

### Open issues

- [Question that genuinely needs owner input, impact, and default if unanswered.]

## Handoff notes

- Last verified: [date/time and commit or source snapshot].
- Verification: [commands run and short outcomes].
- Known limitations or recovery steps: [plain language].
- Next useful business change: [one small next step].
- Owner-facing warning: this app exposes trusted dashboard/tool capabilities to
  reachable LAN clients and has no authentication.
