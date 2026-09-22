# MCP authentication

MCP uses an independent access policy. Existing applications default to public
MCP. Enable `toolframe-mcp-auth` with the SQLite provider, then use **MCP keys**
in Dashboard Auth to create a named key, select business permissions and optional
expiry, and explicitly choose **protected** MCP access. Creating or revoking a
key never changes access policy. Use the matching SDK, core, FastMCP and Dashboard
Auth builds from this feature when testing before their coordinated release.
Public mode grants anonymous business access and ignores external authorization
headers, preserving existing clients. Key grants are enforced in protected mode.

```toml
[mcp_access]
mode = "protected"
auth_provider = "mcp-auth"

[plugins.mcp-auth]
source = "pypi:toolframe-mcp-auth==0.1.0"
enabled = true
```

The package source above is for the published release. During development, use
the new plugin checkout's absolute path. Apply reviewed TOML edits with
`toolframe apply`; the Dashboard policy control uses a revision-checked edit.
The feature does not silently add a plugin to existing applications or templates.

## Using a key

Each key uses 32 random bytes (256 bits), encoded as `tf_mcp_` plus 43 URL-safe
characters. Copy the 50-character key when it is shown: it cannot be retrieved
again. Configure the client with the public application's `/mcp` URL and send
`Authorization: Bearer <key>` on every HTTP request. Use HTTPS for remote access.
This path supports clients with preconfigured bearer tokens/custom headers.
For OAuth discovery and browser authorization, install the shared OIDC plugin;
see [OIDC authentication](oidc-authentication.md). Keys and delegated OAuth tokens
coexist in protected mode, with independent revocation.

Issue one key per client. Rotation is creating a replacement, updating the client,
then revoking the old key. Expired/revoked keys cannot be restored. There is no
refresh token or user-supplied key. Existing keys do not automatically acquire
new business scopes.

## Administration and storage

Local Control and the management token retain their management authority.
Remote password administrators need `system.auth.mcp_keys`. Issuance, listing
and revocation are limited to grants within that administrator's own business
scopes and expiry; changing policy requires `system.settings.manage`.
Names identify grants/clients rather than user accounts. Issued keys are independent
of their creator; revoking a creator's password does not revoke its issued keys.

The protected core API is `POST /_toolframe/v1/mcp-auth/{action}`, where action is
`catalog`, `create`, `list`, `revoke` or `policy`. Create accepts `label`, `scopes`
and nullable `expires_at` (UTC Unix seconds) and returns `{key, token}` once. List
returns `{keys}` containing safe metadata; revoke accepts `{id}`. Policy accepts
`mode` and `expected_revision`. All responses disable caching. Keys cannot call
these management APIs or sign into the Dashboard.

The plugin owns its managed SQLite store. It saves SHA-256 digests, record IDs,
labels, business scopes, creator references, creation/expiry/revocation timestamps
and last use. Secrets and digests are excluded from metadata responses. Core
does not persist credential operations in the configuration/operation journal.
Back up the plugin's consistent managed store with the application's other data.

## Admission and failures

Core validates the external bearer key and exchanges it for an opaque, in-memory
request credential bound to the active MCP provider instance and current MCP
policy. The private FastMCP listener accepts only that credential, resolves it
through core, and carries it back in resource callbacks. External context headers
are stripped. Contexts are removed when their HTTP request/stream finishes.

Discovery lists only tools allowed by both resource and target-method scopes.
Core independently checks both scopes, expiry and revocation before admitting a
tool call. Nested service calls inherit that principal. Successful admissions
update last use; authentication, listing and denied calls do not. Already admitted
roots finish. Stateful legacy MCP sessions bind to the key record and cannot be
used by a different key; every GET, POST and DELETE still authenticates.

Missing/invalid/expired/revoked keys return HTTP 401 with a Bearer challenge.
Missing tool permissions reject the call before its business method executes.
Protected mode with no keys stays closed. A missing, disabled, reloading or failed
auth provider returns 503 and never switches MCP to public. Loopback MCP clients
also require keys. Changing policy invalidates requests admitted under the previous
policy. Only an explicit operator policy change opens public MCP.

Dashboard public fallback, public business APIs and llms documents retain their
separate access policies. MCP bearer protection does not protect those entrances.

## Verification

`tests/test_mcp_auth.py` exercises real packaged workers, real FastMCP clients,
legacy session binding, direct-listener rejection, core scope enforcement,
delegation, expiry/revocation, failure, reload and complete host restart. The new
plugin's tests exercise storage and credential invariants; Dashboard Auth's
`tests/test_mcp_page.py` covers generation, one-time display, errors and revocation.
