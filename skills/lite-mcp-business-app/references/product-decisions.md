# Product decision history

This is the durable record of the `product_grill` that shaped
`lite-mcp-business-app`. It is decision history for maintainers and reviewers,
not normal activation context. The primary agent answered these questions as
product owner under the user's instruction to show every question,
recommendation, answer, and rationale before implementation.

## Round 1 — audience and operating agreement (Q1–Q4)

### Q1 — Who should this skill serve?

- **Question:** Should the skill teach technical architecture, or let a
  nontechnical owner describe a business outcome while an agent implements it?
- **Recommendation:** Make the interaction agent-led and business-first; ask
  about outcomes, actors, records, review, and completion, then select the
  technical defaults.
- **Product-owner answer:** Agent-led; the user should not need technical
  skills to operate or extend a local business app.
- **Rationale/tradeoff:** This lowers the entry barrier while requiring the
  skill to explain defaults and surface risks in plain language.

### Q2 — What does the executable mean to the owner?

- **Question:** Should the handbook describe `lite-mcp` as a generic server,
  or as a local business handoff tool with a human dashboard and peer-agent
  MCP interface?
- **Recommendation:** Explain it as one LAN-first executable that runs the
  dashboard, SQLite-backed business workspace, and MCP endpoint together.
- **Product-owner answer:** Business-first operational meaning; the user asks
  for work to be managed, viewed, reviewed, and handed to agents.
- **Rationale/tradeoff:** The owner gets a useful mental model rather than a
  framework tutorial; the app's small local scope must be stated plainly.

### Q3 — What lifecycle should the skill cover?

- **Question:** Is a generated demo sufficient, or must the handbook guide
  discovery, implementation, operation, diagnosis, handoff, and extension?
- **Recommendation:** Cover the full lifecycle with a repeatable business-first
  workflow and explicit completion/doctor gates.
- **Product-owner answer:** Include lifecycle guidance and durable docs as
  part of every business-app change.
- **Rationale/tradeoff:** Future agents can safely continue the work; keeping
  the lifecycle current costs documentation effort.

### Q4 — What app-local documents must be left behind?

- **Question:** Should context live only in the conversation, or must the app
  contain documents for the next coding agent?
- **Recommendation:** Create or reconcile `APP_CONTEXT.md` and
  `DOMAIN_MODEL.md` in the editable app folder.
- **Product-owner answer:** Both are mandatory handoff documents covering
  purpose, actors, source of truth, schema, tools, widgets, states, and limits.
- **Rationale/tradeoff:** Durable context prevents drift between business
  language and implementation; the docs must be kept synchronized.

## Round 2 — domain shape and history (Q5–Q9)

### Q5 — What is the canonical business object?

- **Question:** Should the scheduled-agent example use an ad hoc `task` table,
  or a stable canonical object shared by agents and dashboard users?
- **Recommendation:** Use `Job` as the canonical object for the work-desk
  workflow.
- **Product-owner answer:** Canonical `Job`.
- **Rationale/tradeoff:** A stable vocabulary improves interoperability; a
  non-job business app may use its own domain language instead of forcing Job.

### Q6 — Should the skill prescribe one workflow or many?

- **Question:** Should MVP guidance model every business process, or prove one
  bounded loop and let other apps adapt it?
- **Recommendation:** Make scheduled-agent work the one canonical bounded
  workflow: create, claim, read context, progress, result, human review.
- **Product-owner answer:** One bounded workflow first; extend only for a real
  business requirement.
- **Rationale/tradeoff:** One complete loop is testable and teachable; advanced
  workflows remain deliberately outside the MVP.

### Q7 — Who owns schedule, Job, and Attempt?

- **Question:** Should `lite-mcp` implement recurrence and scheduling, or
  distinguish an external schedule, the durable Job, and each agent run?
- **Recommendation:** Keep schedule ownership external. `Job` is the durable
  business request; `Attempt` is one claim/execution; a scheduler decides when
  a Job becomes available.
- **Product-owner answer:** External schedule; keep schedule, Job, and Attempt
  separate.
- **Rationale/tradeoff:** This keeps the executable small and interoperates
  with existing triggers; it does not promise cron, recurrence, or workers.

### Q8 — What context is authoritative?

- **Question:** Should agents read mutable instructions, or an immutable
  version of the user context attached to their Attempt?
- **Recommendation:** Store immutable context versions and bind each Attempt to
  the exact version it consumed.
- **Product-owner answer:** The version attached to the Attempt is authoritative
  for that execution; changes create a new version.
- **Rationale/tradeoff:** Review can reconstruct what the agent knew; owners
  must add a version rather than silently editing history.

### Q9 — Can results be overwritten?

- **Question:** Should progress and results retain only the latest value, or
  preserve every submission and supersession relationship?
- **Recommendation:** Make results and progress evidence append-only; use a
  current-result reference or managed artifact for presentation.
- **Product-owner answer:** Append-only results; never erase an earlier result.
- **Rationale/tradeoff:** History supports review and recovery; large payloads
  need artifact references and retention decisions.

## Round 3 — lifecycle and execution (Q10–Q14)

### Q10 — What is the exact Job finite-state machine?

- **Question:** Which states and transitions must every canonical Job tool and
  dashboard mutation enforce?
- **Recommendation:** Use this exact graph: `draft → ready → claimed →
  in_progress → review → done`; `claimed/in_progress → blocked → ready`;
  `review → changes_requested → claimed`; any active nonterminal state may go
  to `cancelled`; an explicit business `failed` state is terminal.
- **Product-owner answer:** Adopt that exact FSM. Technical failure and retry
  exhaustion do not mean business `failed`; they leave the Job `blocked`.
- **Rationale/tradeoff:** The small directed graph makes guards predictable and
  reviewable. It requires tools to reject unlisted jumps and distinguish a
  technical blockage from an explicit business failure.

### Q11 — What identity model is required?

- **Question:** Should MVP add authentication, or record human/agent actors as
  labels for history while remaining trusted-LAN and unauthenticated?
- **Recommendation:** Record actor labels/IDs on Jobs, Attempts, events, and
  reviews; do not implement authentication.
- **Product-owner answer:** Actor labels, not auth.
- **Rationale/tradeoff:** History is understandable without identity
  infrastructure; any LAN client can still claim a label, so this is not a
  security boundary.

### Q12 — What counts as pending work?

- **Question:** Should pending mean every nonterminal Job, or only work an
  agent may safely claim now?
- **Recommendation:** Pending includes due `ready` Jobs and due
  `changes_requested` Jobs. Expired claims are reclaimed atomically so they
  become claimable again; drafts, blocked work, review, and terminal states are
  not pending.
- **Product-owner answer:** Use due `ready` or `changes_requested` candidates,
  with expired claims reclaimed atomically.
- **Rationale/tradeoff:** Agents do not steal drafts or review work, while
  abandoned claims recover without a manual database edit. “Due” remains a
  business field, not a built-in scheduler.

### Q13 — How are claims protected?

- **Question:** Is read-then-update sufficient, or should claiming use an
  atomic lease and stale-write guard?
- **Recommendation:** Claim in one SQLite transaction, create an Attempt, set
  claimant/lease expiry and a fencing or version value, and reject stale lease
  writes.
- **Product-owner answer:** Use atomic claims, leases, and stale-claim
  rejection.
- **Rationale/tradeoff:** Peer scheduled agents cannot safely share a best-
  effort queue without this; lease expiry requires agents to reacquire rather
  than overwrite newer work.

### Q14 — What is the default retry policy?

- **Question:** Should technical retries be unlimited or configurable from the
  first implementation?
- **Recommendation:** Default to at most three technical execution Attempts per
  Job, including the first Attempt; classify each failure and move exhausted
  technical work to `blocked`.
- **Product-owner answer:** Maximum technical Attempts per Job is 3 by default;
  exhaustion leads to `blocked`, never automatically to business `failed`.
- **Rationale/tradeoff:** A three-Attempt budget handles transient faults
  without an endless loop. A later business app may choose a different policy
  explicitly.

## Round 4 — ownership and writes (Q15–Q18)

### Q15 — Who owns database objects?

- **Question:** May user code modify runtime bookkeeping tables, or must app
  data have a clear ownership boundary?
- **Recommendation:** User business tables and migrations are app-owned,
  conventionally prefixed `app_`; runtime-managed tables/contracts remain
  owned by `lite-mcp`.
- **Product-owner answer:** Keep `app_` ownership explicit and separate.
- **Rationale/tradeoff:** Upgrades and diagnosis are safer; an app cannot rely
  on undocumented runtime internals.

### Q16 — What is the write authority?

- **Question:** Should agents write through arbitrary SQL, or use named
  business commands while retaining the shipped SQL reference?
- **Recommendation:** Use narrow named read/write MCP tools for business
  operations. Keep `tools/execute_sql.py` as a trusted local coding/reference
  tool, not as the domain contract.
- **Product-owner answer:** Named writes are authoritative; retain
  `execute_sql` as a local reference.
- **Rationale/tradeoff:** Named tools are discoverable and safer for normal
  operation; the reference remains useful for inspection and repair and is not
  an authorization boundary.

### Q17 — Can the dashboard mutate data?

- **Question:** Should humans only preview data, or may they add, progress,
  review, and correct business items from Streamlit?
- **Recommendation:** Support dashboard mutations through forms, editors, and
  buttons that call shared helpers and enforce domain transitions.
- **Product-owner answer:** Dashboard mutations are required.
- **Rationale/tradeoff:** Humans can operate without MCP knowledge; the browser
  must not submit arbitrary SQL and every write must be parameterized/audited.

### Q18 — What guarantees apply to mutations?

- **Question:** Is a successful UI/tool response enough, or must writes be
  atomic, idempotent, version/lease guarded, and historically visible?
- **Recommendation:** Use parameterized SQL in an atomic transaction, validate
  state/lease/expected version, record an event, and accept idempotency keys for
  retried mutations; return actionable errors.
- **Product-owner answer:** Require these guarantees for MCP and dashboard
  writes.
- **Rationale/tradeoff:** The contract prevents duplicate or stale updates;
  implementation has more ceremony than a direct SQL button.

## Round 5 — failure and operating boundaries (Q19–Q22)

### Q19 — What happens when a component fails to load?

- **Question:** Should a broken tool or dashboard be omitted with a warning,
  or should startup fail as a whole?
- **Recommendation:** Fail the whole app for migration, MCP tool, or dashboard
  load/contract failures, with component, phase, detail, and retryability.
- **Product-owner answer:** Fail startup and explain why; never expose a partial
  business app that looks healthy.
- **Rationale/tradeoff:** Fail-closed startup is less forgiving but avoids
  accepting work through an incomplete or misleading interface.

### Q20 — How should doctor mode begin?

- **Question:** Should diagnosis edit files or repeatedly restart first?
- **Recommendation:** Start report-first and non-mutating: run the structured
  check, inspect logs and exact failure, preserve migrations/data, then make
  the smallest repair and verify startup.
- **Product-owner answer:** Report first; do not bypass validation or guess.
- **Rationale/tradeoff:** It protects user data and gives a useful agent handoff,
  even though it is slower than blind retries.

### Q21 — How should retryability be explained?

- **Question:** Which failures should an agent retry, repair, or surface as a
  business decision?
- **Recommendation:** Mark transient network/lock/timeout/dependency-readiness
  failures retryable; mark syntax, schema, migration-order/checksum,
  contract, and business-rule failures non-retryable until source is repaired.
- **Product-owner answer:** Every relevant error must state detail, reason, and
  whether retry is appropriate; technical exhaustion maps to `blocked`.
- **Rationale/tradeoff:** Agents get a clear next action instead of retrying
  corrupt input or hiding a defect as a normal business result.

### Q22 — When is a dedicated development app required?

- **Question:** Should this handbook grow to cover public customers, auth,
  multi-tenancy, untrusted code, high availability, or durable workers?
- **Recommendation:** Set a hard escalation boundary for public/internet use,
  auth/permissions, untrusted actors or code, multi-tenant isolation, HA,
  large-scale writes, durable queues/workers, strict compliance, multiple
  databases, and polished real-time/mobile products.
- **Product-owner answer:** Escalate those requirements to a dedicated
  development app.
- **Rationale/tradeoff:** The local product stays quick and honest about its
  trusted-LAN/SQLite limits; unsupported requirements are not patched around.

## Round 6 — LAN, runtime, and packaging (Q23–Q26)

### Q23 — How should peer agents discover the app?

- **Question:** Should peers choose among every interface reported by the
  machine, or receive one usable LAN route from the executable?
- **Recommendation:** Use the printed preferred, tested LAN address; peers
  must not use loopback or an arbitrary machine-interface assignment.
- **Product-owner answer:** Peers use the printed preferred tested LAN address,
  not loopback machine assignments.
- **Rationale/tradeoff:** This gives nontechnical owners one instruction and
  avoids dead virtual/link-local routes; diagnosis can still inspect details.

### Q24 — What endpoint should be advertised?

- **Question:** How should the dashboard and MCP URLs be presented to humans
  and peer agents?
- **Recommendation:** Advertise the preferred LAN base URL, dashboard at `/`,
  info at `/info`, and MCP at `/mcp`; use localhost only for same-machine
  access and do not advertise SSE or stdio.
- **Product-owner answer:** Use the tested LAN URL plus `/mcp` for peers and
  the same base URL for the human dashboard.
- **Rationale/tradeoff:** One port and one URL family simplify setup; the MVP
  remains LAN-only, unauthenticated, streamable HTTP.

### Q25 — What makes the app portable?

- **Question:** Should users assemble Python services and dependencies, or hand
  an agent one editable folder with a self-contained runtime contract?
- **Recommendation:** Keep one Rust launcher, one UV-managed Python process,
  Python 3.14, one port, SQLite `data.db`, and editable `migrations/`,
  `tools/`, and `dashboard/`; resolve/use the bundled or available UV path.
- **Product-owner answer:** Prefer the portable one-folder experience and one
  process; no extra service, SSE, stdio, or MCP hot reload.
- **Rationale/tradeoff:** Setup is predictable for a nontechnical owner; tool
  changes still require restart and the app is not a general deployment stack.

### Q26 — Should the handbook ship with the binary?

- **Question:** Is a repository-only skill sufficient, or must packaging make
  the executable and its business-first handbook travel together?
- **Recommendation:** Package the single skill at
  `skills/lite-mcp-business-app` alongside the editable distribution, with
  source as canonical.
- **Product-owner answer:** Ship the skill with the distribution.
- **Rationale/tradeoff:** An agent receives both capability and guidance in one
  folder; packaging adds a source/package reconciliation check.

## Round 7 — proof and completion (Q27–Q30)

### Q27 — What is the canonical end-to-end proof?

- **Question:** What must be demonstrated before the scheduled-agent work desk
  is considered usable?
- **Recommendation:** In a fresh editable distribution, create the migration
  and docs; start the app; have a peer use `/mcp` to claim due work, read the
  immutable context, record progress, submit a result, and have a human review
  it in the dashboard.
- **Product-owner answer:** Use that create-to-review flow as the canonical
  E2E.
- **Rationale/tradeoff:** It proves shared durable state, leases, MCP, LAN,
  dashboard mutations, and review history in one business loop.

### Q28 — What makes an implementation complete?

- **Question:** Can the agent stop after code generation, or must it leave a
  coherent app handoff?
- **Recommendation:** Completion requires the implemented schema, narrow tools,
  dashboard widget, `APP_CONTEXT.md`, `DOMAIN_MODEL.md`, limitations, and a
  report of the verified business workflow.
- **Product-owner answer:** Require the complete handoff, not just source code.
- **Rationale/tradeoff:** Another agent can continue safely; maintaining all
  surfaces together takes deliberate effort.

### Q29 — Which tests and checks are completion gates?

- **Question:** What evidence must accompany “done”?
- **Recommendation:** Run focused migration/domain/tool/dashboard/idempotency
  and error-path tests, repository formatting/lint, `./lite-mcp check --format
  json`, then verify startup, `/info`, `/`, `/mcp`, and the printed LAN URL.
- **Product-owner answer:** Tests and runtime checks are mandatory gates.
- **Rationale/tradeoff:** This catches an unstartable dashboard, broken tool, or
  source/runtime mismatch before handoff.

### Q30 — What is the source of truth at handoff?

- **Question:** May an agent edit applied migrations, patch `data.db` silently,
  or update only generated/package files to get a green result?
- **Recommendation:** Preserve applied migration history; add the next
  contiguous migration; keep source files canonical; reconcile docs, code,
  generated artifacts, and package contents before reporting completion.
- **Product-owner answer:** Source, migrations, docs, tests, and package must
  agree; never bypass validation or silently rewrite user data.
- **Rationale/tradeoff:** A fresh distribution remains reproducible and existing
  data remains protected, even when a repair needs a new migration or explicit
  recovery plan.

## Round 8 — integration recovery and skill delivery (Q31–Q32)

### Q31 — What should a workflow return when MCP cannot be reached?

- **Question:** Should an unreachable LAN MCP endpoint become a Job failure, a
  silent scheduler retry, or a visible integration warning?
- **Recommendation:** Pause before business-state handling, return a visible
  warning, and include one self-contained Markdown issue the owner can copy.
  Record exact observed connection evidence and leave unknown fields explicit.
- **Product-owner answer:** Warn in the workflow response and provide a
  copyable issue whenever the server is stopped or the agent cannot access MCP.
- **Rationale/tradeoff:** The agent cannot know or mutate the current Job state
  without MCP. A fixed handoff gives the owner actionable evidence while
  avoiding a false business failure or a silent missed schedule.

### Q32 — How should the bundled skill become discoverable?

- **Question:** Must the owner manually install the shipped handbook, or should
  normal server startup place a production copy in the app's agent-skill path?
- **Recommendation:** Atomically create
  `.agents/skills/lite-mcp-business-app` beside the executable on normal server
  startup. Preserve an existing directory, fail on unsafe path types, and keep
  source/evaluation material separate.
- **Product-owner answer:** Auto-install the skill under `.agents/skills`; omit
  `evals/` and `evaluations/` because they are not required to run the skill.
- **Rationale/tradeoff:** Coding agents discover the handbook without a manual
  setup step. Create-once behavior protects local edits; a later release does
  not silently upgrade an existing installed copy.

## Round 9 — LAN agent discovery (Q33–Q34)

### Q33 — How should a peer learn the app from one URL?

- **Question:** Should every runtime and business detail live in one root
  document, or should the app expose a small `/llms.txt` index with linked
  detail pages that a peer reads only when relevant?
- **Recommendation:** Follow the progressive llms.txt shape: a concise root
  index at `/llms.txt` and editable app-specific documents below `/llms/` for
  the agent guide, capabilities, context, workflow, and operations.
- **Product-owner answer:** Use the subpath approach. Ship a default template
  like tools and Dashboard source, then let the coding agent adapt it to the
  user's business requirement without placing every detail in `llms.txt`.
- **Rationale/tradeoff:** A peer receives one stable copy/paste URL without
  wasting context on unrelated detail. The coding agent must keep the linked
  projection aligned with actual tools, widgets, schema, and domain documents.

### Q34 — What outcome should the guide drive?

- **Question:** Is reading the guide sufficient, or should it direct the peer
  to connect this server as MCP and build the requested workflow from verified
  capabilities?
- **Recommendation:** State same-origin `/mcp` and Streamable HTTP in
  client-neutral terms, require the peer to use its supported MCP registration
  flow, and treat a successful tool-list response as proof before building a
  bounded workflow.
- **Product-owner answer:** When another agent receives `LANIP/llms.txt`, it
  should recognize this as an MCP-backed app, try to install/register it, and
  build the user's workflow from the published capabilities.
- **Rationale/tradeoff:** MCP client commands and approval policies vary, so
  the guide cannot guarantee automatic installation or invent one universal
  command. It can provide the exact name, same-origin URL, transport, proof
  step, and a precise blocker when the current client cannot register it.

## Round 10 — application identity (Q35)

### Q35 — Is Lite MCP the app name or the framework name?

- **Question:** Should every fresh local app present itself as Lite MCP, or
  should Lite MCP remain framework attribution while each owner's business app
  receives a distinct name?
- **Recommendation:** Initialize owner-facing identity as the visible sentinel
  `Unnamed`. During development, proactively propose two or three
  business-specific names with one recommendation, then apply the owner's
  choice or a clearly recorded provisional recommendation consistently across
  the Dashboard, app context, and LAN agent guide.
- **Product-owner answer:** Lite MCP is only the framework name. The Dashboard
  starts as `Unnamed` so the missing identity is noticeable, and the
  development agent must suggest an app name automatically.
- **Rationale/tradeoff:** Distinct names let humans and peer agents identify
  several LAN apps without confusing framework branding for business purpose.
  A provisional recommendation keeps development moving, while the owner
  retains the final naming decision.
