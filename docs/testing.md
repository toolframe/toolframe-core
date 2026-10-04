# Testing and release qualification

Run checks against the exact revision and package set being released. Verification
requirements are not certificates that every scenario has passed on every platform.
Record a run's versions, commands, outcomes and limitations in its CI artifacts or
release evidence.

## Local checks

From Core, with the sibling repositories available:

```sh
uv run pytest -q
uv run ruff check src tests scripts/verify-release.py
cargo test
cargo fmt --check
```

Use the isolated commands in [CI and release preparation](ci.md) for pinned-source
validation and distribution checks. Run package tests in each changed sibling.
Architecture tests enforce public import boundaries; skill, scaffold and CLI tests
cover delivery of the authoring guide and independently buildable starter. The
skill's `evals/cases.json` remains a development input and is excluded from installed
skill payloads. Structural delivery checks do not measure model routing or
independent agent implementation quality.

The real SQLite maintenance inventory regression accepts explicit SDK and SQLite
provider wheels, so it can run from a worktree without sibling source checkouts.
Use a prepared Core development environment and matching wheels:

```sh
TOOLFRAME_SDK_WHEEL=/path/to/toolframe_sdk-0.3.0-py3-none-any.whl \
TOOLFRAME_STORAGE_SQLITE_WHEEL=/path/to/toolframe_storage_sqlite-0.3.0-py3-none-any.whl \
.venv/bin/python -m pytest -q \
  tests/test_maintenance.py::test_real_core_sqlite_business_shutdown_has_public_inventory
```

Omitting the SQLite wheel override retains the existing sibling source-checkout
fallback used by developer acceptance tests. Other fixtures keep their existing
package-source selection.

## Executable and integration probes

Build with `bash scripts/build-release.sh`. Probe scripts accept `--help` for their
inputs. Write generated reports under ignored `build/` or `dist/`, and use
disposable application homes rather than an operator's existing application.

| Driver | Coverage |
| --- | --- |
| `scripts/verify-release.py` | Copied executable, empty/demo startup, caches, replacement, shutdown and missing-cache behavior. |
| `scripts/verify-network.py` | Gateway listeners, local management and LAN access. |
| `scripts/verify-auth.py` and `scripts/verify-auth-upgrade.py` | Access rules, scoped operations and upgrades. |
| `scripts/verify-dependencies.py` | Dependency and impact diagnostics. |
| `scripts/verify-operation-history.py` | Journal ordering, pagination and filters. |
| `scripts/verify-retained-plugins.py` | Retained artifacts across binary upgrades. |
| `scripts/verify-skill.py` and `scripts/verify-scaffold.py` | Skill installation, packaging, upgrades and generated plugins. |

Run the release probe with `--uv-path /path/to/uv` to qualify an installed version
against the [runtime range](runtime.md). It also asserts that no private uv was
downloaded. Cover the minimum supported version and a newer supported patch, and
run without a compatible uv on PATH to exercise the pinned managed download.

The FastMCP repository supplies gateway and Codex client probes. The Codex probe
exercises its MCP client API, not a model turn or desktop GUI. Real browser checks
must cover reconnects, navigation and retained business data separately from
Streamlit AppTest.

Native arm64 and Intel under Rosetta are distinct environments. Physical Intel,
the macOS 12 minimum, independent desktop MCP clients, signing/notarization and
remote CI/publication require their own evidence. Linux/Windows application
support, multi-instance deployment, artifact storage and full business
audit providers are outside this version. Warm-cache offline startup does not
establish a fully offline first installation.

## Release scenarios

Record composition and lock revisions, exact package/content/environment IDs,
worker generations, operation phases, catalog epochs and relevant database
revisions. Exercise public management paths as well as lower-level unit checks.
Report unexecuted or blocked scenarios explicitly. Each release's evidence must
identify which rows were actually exercised.

| Area | Required scenarios and observable outcomes |
| --- | --- |
| Empty host | Management, core SQLite, liveness and diagnostics work without optional provider imports. |
| Package sources | Local, Git, wheel and index requests produce immutable candidates; moving refs resolve explicitly and installation remains disabled unless activation is requested. |
| Validation | Bad source, imports, dependencies, resources or UI conflicts leave the active generation intact; private runtime validation uses isolated stores. |
| Dependency lifecycle | Missing required providers block consumers without hiding management; independent workers retain their generations during scoped operations. |
| Reviewed operations | Changed composition, candidate, generation or schema invalidates the plan; concurrent mutations cannot perform competing cutovers. |
| Drain and cancellation | Admitted nested calls and transactions complete against their leased generation; timeout/cancellation before disposal resumes intact workers. |
| Cleanup and forced stop | Surviving owners prevent replacement; explicit forced stop fences the affected generation and reports interrupted or uncertain calls. |
| Failed activation | Compatible old snapshots may recover once after proven cleanup with unchanged schema; recovery remains a failed operation rather than a successful update. |
| Durable stores | Disable, uninstall and compatible reinstall preserve data and migration history; cross-owner access goes through services. |
| Migration | Existing schemas require an explicit checked plan and backup; failed SQL rolls back allowed work, while committed-schema activation failure requires forward repair. |
| Crash recovery | Interrupt before and after schema commit and journal updates; authoritative schema and receipts prevent duplicate migration or false success. |
| Dashboard replacement | Two real browser sessions reconnect through the gateway to the selected page/navigation generation; transient session state may reset while business data persists. |
| MCP replacement | Changed catalogs and schemas appear after explicit reconnect; removed tools fail. Exercise two FastMCP clients and an independent desktop MCP client separately. |
| MCP continuity | Backend-only changes with unchanged descriptors preserve the eligible provider; long calls drain and writes are not silently replayed. |
| Document publication | Index and declared document URLs match the selected epoch; withdrawn owners and unrelated package files are not served. |
| Partial publication | An unacknowledged provider remains unavailable, with visible degradation and reconciliation against the selected epoch. |
| Watch | Bursts coalesce; a repeated failed digest does not loop; disabled/stopped plugins, migrations, forced termination and Core restarts require explicit action. |
| Dependency environments | Worker/native changes prepare fresh environments; incompatible protocols and Core-interpreter changes require rejection or restart. |
| Status and health | Missing, stale and failed probes are explicit; optional degradation and required-capability readiness remain separate from host liveness. |
| CLI/API/Dashboard parity | Shared plans, IDs and durable outcomes survive client disconnect and Dashboard replacement; errors and exit codes match the operation result. |
| Worker protocol | Nested concurrent calls, cancellation, invalid/oversized frames, lost pipes and stale generations produce bounded errors and cleanup without deadlock. |
| Plugin independence | Clean built wheels load resources without source checkouts, private imports or special demo behavior. |
| Distribution | Empty/demo applications pass on each advertised architecture; warm offline startup uses exact cached inputs and missing-cache startup fails clearly. |
| uv selection | Validate the runtime range's minimum, the managed pin and a newer supported patch; explicit selection precedes PATH, compatible PATH precedes managed/download, invalid explicit selection fails, and offline reuse avoids downloads. Test renamed executables through both bootstrap and plugin preparation. |
| Access | Public/protected policies, expiry/revocation, explicit scopes, local control, auth failure and recovery match the documented access contract. |

## Entry Register behavior

Labels are trimmed, nonempty and at most 80 characters. Numeric values must be
finite; negative values and zero are valid. Integer IDs start with version 1;
conditional updates increment the version and use UTC timestamps. Missing rows
and stale versions produce conflict/refresh guidance. The UI rejects no-change
edits.

Dashboard displays at most the newest 1,000 rows; count and total describe those
displayed rows, with total shown to two decimal places. The read-only MCP list
defaults to 50, permits 1–100, rejects invalid and boolean limits, and returns
entries/count/limit/nextAction. The wider business-service list limit does not
change the tool's bound.

Use ordinary test fixtures for another data owner, required-service consumers,
slow/failing workers and schema upgrades. Cross-owner SQL rejection verifies
trusted-plugin ownership guards, not an operating-system sandbox.

## PostgreSQL management journal

The default suite exercises SQLite and skips database-backed Core cases without
an isolated `TOOLFRAME_CORE_TEST_ADMIN_DSN`. To require real PostgreSQL validation,
install the `postgres` extra and the pinned PostgreSQL storage provider, then run
`python scripts/ci/check-postgres.py`. Use `pg_dump` and `pg_restore` matching the
server's major version. The
reusable workflow runs a separate PostgreSQL 16 service job. This check fails when
its DSN, driver or provider is absent; it covers both journal adapters, lost acknowledgements,
cancellation, connection failure, ownership recovery and worker shutdown.

`tests/test_mixed_storage.py` installs SQLite and PostgreSQL provider wheels into
real workers. It covers explicit/default routing, owner isolation, standalone and
transactional writes, provider reload/drain, disable without fallback, retained
reinstall/restart, rejected reassignment, stale migration tokens and recovery after
lost migration acknowledgements on both backends. `tests/test_storage_bindings.py`
covers graph constraints, journal adoption, admitted binding snapshots and cleanup
when one provider fails. Installed Dashboard and MCP client flows still require
separate application verification.

Internal host tests construct hosts with `await Host.create(...)` and await journal
and management methods. SQLite-only migration tests may inspect `SQLiteCoreState`
directly; production callers use the asynchronous `CoreState` interface.
