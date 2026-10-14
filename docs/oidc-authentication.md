# OIDC sign-in and delegated MCP access

Core can route a shared `toolframe-oidc-auth` provider for both Dashboard sign-in
and third-party Agent consent. Auth owns local identities/sessions/grants; OIDC
Auth uses Authlib for protocols and opaque token persistence; MCP Auth optionally
verifies those tokens alongside its own keys. Install coordinated builds of SDK,
Core, Auth, Dashboard Auth, MCP Auth and the OIDC plugin. This optional plugin is
not part of the Entry Register template; installing Core does not enable it.

## Gateway configuration

Keep the application's existing ID and plugin settings, then configure a fixed
public origin and the selected provider:

```toml
[application]
public_url = "https://app.example"

[access]
mode = "protected"
failure_mode = "closed"
oidc_provider = "oidc-auth"

[mcp_access]
mode = "protected"
auth_provider = "mcp-auth"
```

The OIDC plugin's own `public_url` must equal the application origin. Its README
specifies upstream providers, private client-secret files, exact issuer/subject
admission in Auth, static clients, dynamic registration and allowed business
scopes. Google can be used directly or behind a Keycloak broker. The same local
identity is used by both Dashboard and Agent flows; provider tokens are never
used as app bearer credentials. Only users admitted by the Auth configuration
can sign in; an email address is not an identity-link key.

For existing Auth v1 stores, make consistent backups, stage the new Auth package,
review `toolframe --home APP migrate plan auth`, then apply its returned plan with
`toolframe --home APP migrate apply auth --plan-id PLAN_ID`. The additive v2
migration retains password records and sessions. Enable the new plugin through a
complete TOML declaration followed by `toolframe --home APP apply`.

HTTPS terminates at a reverse proxy preserving the canonical Host. Core still
uses its configured numeric listen address. It does not trust forwarded identity,
issuer, host or scheme values. A public-host request has no local-operator
privilege even if its network peer is loopback; requests bearing forwarded headers
also lose automatic local control. A direct loopback operator connection and the
management token remain recovery paths. HTTP public origins are permitted only
for loopback development.

## Public endpoints and admission

| Route | Purpose |
| --- | --- |
| `/_toolframe/oidc/providers` | Safe Dashboard login choices |
| `/_toolframe/oidc/login/{provider}` | Start upstream OIDC login |
| `/_toolframe/oidc/callback/{provider}` | Verify upstream response and establish local session |
| `/oauth/authorize`, `/oauth/consent` | Agent request review and browser-bound consent |
| `/oauth/token`, `/oauth/register`, `/oauth/revoke` | Authlib token/registration operations and grant revocation |
| `/.well-known/oauth-authorization-server/oauth` | Metadata for issuer `PUBLIC_ORIGIN/oauth` |
| `/.well-known/oauth-protected-resource/mcp` | Canonical MCP resource and issuer metadata |

MCP 401 challenges advertise resource metadata when OIDC is configured. OAuth
clients discover the AS, register or use a static client, obtain user consent,
and exchange a code using PKCE S256. Both authorization and token requests bind
`resource` to the canonical `/mcp` URL. Core validates delegated user/client/grant/
resource bindings and business-only scopes, then reuses the existing FastMCP
trusted-request mechanism. Tokens cannot invoke Dashboard/management APIs.
Every new MCP operation rechecks current token and grant authority. Already
admitted roots finish under the existing lifecycle contract.

Only OAuth machine endpoints and metadata expose noncredentialed CORS. External
browser entry is allowed for authorization/login/callback GETs. Consent POSTs,
Dashboard and management routes retain origin checks. The canonical Dashboard
root and login page accept top-level GET navigation after an IdP redirect, whose
`Sec-Fetch-Site` remains `cross-site` throughout the redirect chain. This exception
requires `Sec-Fetch-Mode: navigate` and `Sec-Fetch-Dest: document`; it does not
admit fetches, frames, other routes, writes or local-control URLs. Dashboard
session admission still applies. HTTP bodies are bounded;
OIDC transaction/session cookies are HttpOnly, Secure on HTTPS, and SameSite=Lax
for cross-site OIDC returns. Password/operator cookies remain SameSite=Strict and
are Secure when a canonical HTTPS origin is configured. Core strips these cookies
before forwarding to presentation workers.

## Local management and failures

`POST /_toolframe/v1/auth/{action}` adds `user_catalog`, `users`, `user_update`,
`grants` and `revoke_grant`. User administration requires `system.auth.users` and
is bounded by the administrator's permissions and expiry. `user_update` takes
`id`, explicit `scopes`, boolean `disabled` and nullable Unix `expires_at`.
Self-service grants require `system.auth.self` and an actual user identity;
`revoke_grant` takes `id`. Users cannot revoke someone else's grant. Disabling a
user invalidates sessions and revokes their Agent grants. Reducing scopes affects
subsequent admissions. Revoking a grant leaves Dashboard signed in.

Set `failure_mode="closed"` for a protected OIDC deployment. Auth or Dashboard
Auth failure then returns unavailable instead of opening business access.
The default `"public"` preserves older applications' fallback behavior. Invalid
credentials and IdP login failures are denials/unavailability, not a reason to
change policy. An absent or unhealthy optional OIDC verifier does not block
independent MCP keys; protected OAuth requests remain closed. The IdP does not
need to be online to validate an unexpired local token and grant.

Explicitly public business APIs and llms documents keep their own existing access
rules. This feature adds delegated-token admission to MCP; it does not implicitly
protect or enable token authentication on every app route. There is no CIMD,
confidential downstream client, device grant, client-credentials flow or global
IdP logout in this release. See [the architecture decision](adr/0008-shared-oidc-authentication.md).

## Verification

`tests/test_oidc.py` uses packaged plugin workers to check discovery, callback
rejection, canonical host authority, CORS, malformed registration, optional OIDC
failure with functioning keys and closed Dashboard failure. The OIDC plugin's
TinyAuth fixture additionally drives a real installed application and FastMCP
OAuth client, including upstream sign-in, consent, tool execution, refresh and
revocation. Browser trust/cookie/UI verification is a separate acceptance step.
