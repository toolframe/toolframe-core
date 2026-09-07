# App context

> Keep this private file beside `toolframe`. It is development context for the
> master agent, not a document served to peer agents. Replace every bracketed
> prompt with a concrete fact; delete prompts that do not apply.

## App identity

- App name: [a short business-specific name; never use Toolframe as the app name]
- Naming rationale: [the business noun or outcome that makes this app recognizable]
- Framework: Toolframe

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
installed `toolframe-business-app` skill's `references/job-domain-model.md`.
If the skill is bundled in this app folder, record its actual relative path;
do not assume one when it was installed separately. Simple record keeping,
lookup, or approval alone does not require that model. Do not invent a second
meaning for a canonical term.

## Interfaces

- Dashboard: `/` — [what a human sees first and what can be submitted].
- Built-in information: `/info` — [when to use it].
- Agent guide: `/llms.txt` — [what a new LAN agent should discover and which
  linked pages apply to this business app].
- MCP: `/mcp` — [the small Business RPC catalog, separated into bounded
  business queries and guarded business commands; state each caller intent].
- Database: [selected provider and safe target; SQLite default `data/data.db`].
- Scheduler boundary: [external trigger, cadence, or “none”].

## Boundaries

This app is a trusted, LAN-only, unauthenticated local service. It is suitable
for [trusted users and scale]. It is not suitable for [public access,
authentication/permissions, untrusted code, high availability, or any other
known escalation]. Never put secrets or high-stakes data here unless the owner
has consciously accepted the local exposure.

## Operating instructions

```sh
./toolframe check --format json
./toolframe
```

- Dashboard URL: [copy the URL printed for this run].
- Agent guide URL for a peer: [use the tested printed LAN host/port with
  `/llms.txt`; the peer derives same-origin `/mcp` and proves it by listing
  tools].
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

Domain code consumes another capability through the root public service contract
or a lease-owning business function. Record its runtime dependency/lease as well
as the Python import; leave this empty when no cross-domain capability is needed.

| Consumer domain/plugin | Public contract | Runtime ownership | Business reason |
| --- | --- | --- | --- |
| [domain/plugin or none] | [`from domains.<domain> import KEY`] | [`requires`, `ctx.require` or complete call-time lease, and domain binding] | [why needed] |

| Business capability | Source of truth | Notes for the next agent |
| --- | --- | --- |
| App composition | `app/dashboard.py` | [registry use or custom composition and visible app identity] |
| Capability assembly | `app/plugins.py` | [PLUGINS, DOMAIN_PLUGINS, optional packaged SCHEMAS; automatic domain gate connected to its real business service] |
| Business service | `domains/<domain>/model/{contract,service,plugin}.py`, root `__init__.py` | [public ServiceKey/Protocol, concrete Plugin ID, requires, config/lifecycle and shared MCP/UI methods] |
| Shared dependencies | `app/pyproject.toml` | [why each app-wide package is required] |
| Domain model/schema | `domains/<domain>/model/`, `domains/<domain>/migrations/` | [owning domain, schema target and legacy history, and immutable-history note] |
| MCP tools | `domains/<domain>/tools/` | [narrow commands/queries, intents, and private `_*.py` helpers] |
| Domain UI | `domains/<domain>/ui/` | [auto-loaded direct widgets and manual composition choices] |
| LAN agent discovery | `app/llms.txt`, `domains/<domain>/llms/*.md` | [explicitly linked peer documents and last reconciliation] |
| Large results/files | `workspace/artifacts/` | [artifact naming and retrieval convention] |

For each capability, distinguish its ServiceKey, concrete Plugin ID, schema owner
and automatic `domain.<name>` gate. Record stop/blocked/recovery and retained-data
evidence. Local source is the default; record a package/version only when separately
installed. Skill updates are export/compare/merge work: normal serve preserves an
existing `.agents/skills/toolframe-business-app`, including personal modifications.

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
