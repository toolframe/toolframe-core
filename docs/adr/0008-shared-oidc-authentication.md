# Share identity and delegation above transport authentication

Use one optional OIDC Auth plugin above the existing Auth authority. The plugin
uses Authlib as an upstream OIDC client and downstream OAuth authorization server.
It supports direct providers such as Google and brokers such as Keycloak through
the same discovery-based interface. Protocol and provider changes remain outside
business plugins and MCP's transport-specific credential adapter.

Auth owns durable local users, exact issuer/subject links, sessions and revocable
Agent grants. OIDC Auth owns client registrations, encrypted browser transactions,
codes and opaque token digests. MCP Auth delegates OAuth verification through an
optional service dependency and retains its existing keys. Core owns public
routes, canonical origin, cookie admission and validation of trusted principals.

This keeps Google identity proof distinct from Toolframe authorization. The
upstream token is neither forwarded to tools nor accepted as a Toolframe access
token. A user must explicitly consent to a client/resource/scope grant. Revoking
that grant does not log the user out; reducing/disabling local authority affects
future Agent admissions without relying on an upstream logout event.

Authlib supplies OAuth validation, PKCE and OIDC/JWT handling. Toolframe still owns
consent, storage, identity admission, token lifetime and revocation policy. Using a
library does not make these application responsibilities disappear. Public clients
use authorization code + S256; static registration and bounded DCR are supported.
CIMD and additional grant/client types remain future extensions.

Online opaque-token validation trades independent JWT verification for immediate
local permission/revocation checks and simpler key distribution. It requires the
local Auth/OIDC chain to be available for OAuth requests. Optional-dependency
isolation keeps independently issued MCP keys usable during an OIDC outage.

Protected federated deployments can select `access.failure_mode="closed"`.
Public fallback remains compatible with earlier deployments;
[ADR 0011](0011-safe-new-application-access.md) selects closed policy for new applications. This
extends [ADR 0003](0003-public-fallback-on-auth-error-or-disablement.md); it does not
change explicitly public APIs or llms documents. Exact user admission is initially
IaC-driven; email matching and unreviewed auto-provisioning are excluded.
