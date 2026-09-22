# Architecture

Toolframe runs independently packaged Python plugins behind a persistent control
host. A plugin update replaces captured workers and their owned resources while
the host keeps management and diagnostics available. Plugins execute trusted
Python code; process separation provides lifecycle and dependency isolation.

## Ownership and dependency direction

| Component | Owns |
| --- | --- |
| Rust launcher | Runtime preparation, embedded wheels, architecture selection and invocation of the Python CLI. |
| Core | Composition, package capture, worker supervision, dependency graph, gateway, core SQLite and durable management operations. |
| fastapi-cordis | In-process dependency lifecycle, service leases, blocking and cleanup, connected to workers by Core's adapter. |
| SDK | Public plugin definitions, worker protocol, service proxies, resource declarations, page bridge and scoped storage clients. |
| SQLite provider | Business stores, SQL transactions, migration history and mutation receipts. |
| Dashboard provider | Shared Streamlit environment, navigation, page sessions and management presentation. |
| FastMCP provider | MCP tool adapters and Streamable HTTP sessions. |
| llms.txt provider | Composed index and explicitly declared package documents. |
| Auth / Dashboard Auth | Credential and session ownership / login and password-management presentation. |
| Business plugin | Domain operations, schema assets and optional page, tool and document contributions. |

Core depends on SDK and Cordis. Plugins depend on the public SDK and their own
implementation libraries. Core and SDK do not import presentation providers;
plugins collaborate through service contracts rather than private imports or
shared database connections. Entry Register follows the same contracts as an
independently authored plugin.

An empty application needs no presentation or business-storage provider. Core owns
its database directly, so a failed optional provider does not remove management.
Required service dependencies block consumers when unavailable; optional
presentation dependencies affect the corresponding entrance.

## Composition, packages and identity

`toolframe.toml` is the desired-state authority: sources, enabled intent,
configuration and bindings. `toolframe.lock` records exact resolved artifacts and
environments. Core SQLite stores candidates, observed generations, validation
results and operations. It does not override the composition file.

A distribution exports one `toolframe.plugins` entry point and one stable plugin
ID. Distribution name, import namespace and plugin ID are distinct. A local
directory, Git revision, wheel or package-index request is captured into an
immutable candidate. Git refs resolve to full commits. Source capture excludes
runtime data and rejects changing inputs or escaping paths. Workers execute built
wheels, not live editable source trees.

Package version, source digest, wheel digest, environment identity, configuration
revision and active generation describe different things. A same-version source
edit can produce a new candidate. Validation applies to those exact inputs, not
to later edits sharing the same version label.

`plugin add` installs and validates but leaves activation disabled unless
`--enable` is specified. `update` resolves and stages a candidate; `reload`
selects it through the lifecycle contract. Manual composition edits require
explicit reconciliation. Revision checks prevent management from overwriting a
concurrent edit. Invalid composition remains visible through diagnostics.

## Workers, reload and recovery

Backend workers have separately resolved environments. Page contributors share
one aggregate Dashboard environment, which must resolve before switching. A page
package or shared UI dependency change can replace that entire Dashboard process
and reset transient session state.

A reviewed plan binds the candidate, composition revision, generations, dependency
graph, catalog and schema observations. Changed inputs invalidate the plan. One
management writer and the application-home host lock prevent competing cutovers.
The affected set includes required dependents and presentation cohorts; unrelated
workers remain available.

The lifecycle proceeds through preparation, quiescence, draining, disposal,
activation, readiness and publication. Admitted roots retain their generation
bindings through nested service calls and transactions. Quiescence closes new
admission without abandoning admitted work. A session is not an unlimited lease;
provider replacement can close HTTP streams, WebSockets and MCP sessions.

Preparation failures preserve the active generation. Ordinary drain timeout
resumes intact workers and fails the operation. Cleanup must establish actual
worker exit before another exclusive owner starts. `plugin stop --force` is an
explicit repair action, separate from ordinary reload or disable.

After proven cleanup, a failed activation with unchanged schema may attempt the
compatible old snapshots once. Successful recovery is still a failed operation
(`failed_recovered`), with fresh generations. Committed or uncertain migrations
require forward repair; old code or backups are never restored automatically over
newer data. Client disconnection does not cancel a durable operation. Explicit
cancellation is processed at a safe lifecycle boundary.

On host restart, persisted process states are historical observations. Core
reconciles worker ownership, interrupted operations and actual schema state before
starting owners. Watch uses the same capture/validation/reload path and cannot
implicitly enable disabled plugins, migrate existing data, force termination or
restart Core. Core, interpreter and incompatible protocol changes require restart.

## Data ownership

Each application has `data/core.sqlite`; each persistent business owner has a
retained `data/plugins/<plugin-id>/data.sqlite`. Identity follows the stable owner,
not its package version or worker generation. Disable and uninstall retain data.
Other plugins access it through the owner's public service.

Scoped SQL clients provide parameterized operations and provider-owned
transactions. Transaction identity is bound to owner, generation and admitted
call. Lost workers or failed transactions roll back uncommitted work. Mutation
receipts help distinguish a committed write from a lost acknowledgement; unknown
outcomes must not be replayed blindly. Cross-store atomic transactions and general
SQLite-to-PostgreSQL portability are outside this contract.

A new empty store can initialize its declared schema. Existing data needs an
explicit migration plan bound to the candidate and immutable SQL checksums, a
consistent backup, and reviewed application. Schema/history updates commit in the
owning store. Core's operation journal and the business database are separate
transactions, so recovery checks the authoritative schema and receipts.

## Resource publication and gateway

Plugins declare owned pages, tools and documents. Providers prepare a complete
catalog, then acknowledge the selected epoch before Core makes their entrance
available. A failed acknowledgement remains a visible unavailable entrance;
publication is not presented as a distributed atomic transaction.

Native Streamlit pages call business services through the SDK page bridge. MCP
tools declare schemas and target business methods; the provider does not import
business implementations. Catalog or schema changes replace the MCP provider and
require explicit client reconnection. Unchanged tool metadata can permit a
backend-only replacement without replacing that provider.

The llms provider serves explicitly declared package documents. It does not scan
repositories for arbitrary Markdown. Withdrawal removes the owning plugin's
entries. Core reserves management and health routes and owns HTTP/WebSocket
admission; providers own their application lifespans.

## Management and access

CLI, API and Dashboard share durable plans, operation IDs and outcomes. Status
separates installed candidate, active generation, validation, health and operation
failure. Optional degradation is distinct from required-capability readiness;
`/healthz` reports host liveness. Historical successful validation does not certify
current business health.

The gateway enforces access and carries trusted caller context. Credential
ownership remains in Auth and UI ownership in Dashboard Auth. Current configuration,
public entrances, protected access and recovery rules are defined in
[authentication](authentication.md); their tradeoffs are recorded in the
[access ADRs](adr/0001-explicit-dashboard-access-mode.md).

See the [glossary](glossary.md) for terminology, [dependency diagnostics](dependency-graph.md)
for operator views and [testing guide](testing.md) for verification requirements.

## Shared federated authentication

The optional OIDC Auth provider sits above local Auth and supplies Authlib-based
upstream identity and downstream OAuth endpoints. Core owns HTTP routing and
canonical origin checks; MCP Auth consumes its optional token-verification
service. Business implementations remain scope-based.
[OIDC authentication](oidc-authentication.md) describes configuration and routes;
[ADR 0008](adr/0008-shared-oidc-authentication.md) records ownership and tradeoffs.
