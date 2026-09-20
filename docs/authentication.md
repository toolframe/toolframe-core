# Dashboard access and authorization

Core 0.3 / SDK 0.2 use explicit scopes. Standard bundles include `toolframe-auth`
and `toolframe-dashboard-auth`; Entry Register's template enables both. An empty
`init` remains empty. Updating a binary alone never adds these plugins to an
existing application's composition.

Open the Dashboard directly on `http://127.0.0.1:9876`. The header shows **本機控制**.
The **Access passwords** page creates named grants with random or custom secrets,
optional expiry, selected scopes and a dynamic **All business scopes** option.
System scopes start unchecked. New passwords accept 8–32 characters. The form
supplies an editable unnamed label and a random password placeholder; leaving the
password blank uses that exact suggestion. Copy the password when shown after
creation. Existing longer passwords remain valid for login. **Set expiry** reveals
a duration and unit in two columns, with minutes, hours, days, weeks or calendar
months. Preview and record timestamps use the browser's local timezone. Durations
start when saved; calendar months clamp to the last day when needed. An empty duration when
editing preserves the existing expiry; turning the toggle off removes it. Visitors
enter only the password; names label grants and do not establish a person's identity.
The page lists creation, expiry, revocation, last login and last use, and supports
expiry/grant edits and revocation. Editing a grant invalidates its sessions and
requires a fresh login. Revoked records remain revoked.

Before the first password is created, business access is public with the header
**目前處於公開訪問模式**. First creation changes the saved mode to protected. Removing
all valid passwords does not reopen access. An operator may explicitly choose
public mode afterwards; subsequent password edits respect that choice.

```toml
[access]
mode = "protected"        # public is the default
local_control = true
auth_provider = "auth"
dashboard_auth = "dashboard-auth"

[plugins.auth]
source = "pypi:toolframe-auth==0.1.1"
enabled = true
[plugins.auth.config]
session_max_seconds = 604800
session_idle_seconds = 86400

[plugins.dashboard-auth]
source = "pypi:toolframe-dashboard-auth==0.1.1"
enabled = true
```

Auth requires the SQLite provider. TOML is desired state; use `toolframe apply`
after reviewed configuration edits. The Dashboard's access-policy form uses a
revision-checked edit. If a first-password operation commits its record but the
composition changed concurrently, its response includes the one-time secret,
record ID and `policy_error`; refresh and explicitly select protected mode. Do
not blindly retry credential creation.

## Entrances and grants

| Entrance | Access rule |
| --- | --- |
| Dashboard and `POST /_toolframe/v1/services/OWNER/SERVICE/METHOD` | Effective Dashboard policy and method scopes; internal services excluded |
| `POST /api/OWNER/SERVICE/METHOD` | Public only when the business method explicitly sets `public_api=True` |
| MCP tools and llms documents | Public, independent of Dashboard passwords |
| Management API and plugin management UI | Explicit `system.*` grants, local control or existing management token |
| Internal SQL/auth services | Host/plugin dependency calls only, never generic browser APIs |

`system.plugins.read` permits diagnostics; `system.plugins.manage` controls package
and lifecycle operations; `system.migrations.manage` controls migrations;
`system.settings.manage` edits access policy; `system.auth.passwords` manages
passwords. The password management page requires the latter. Select read access
as well when granting remote plugin management so its status page is usable.

Dynamic business grants include newly installed business scopes and exclude
system scopes. Explicit selections never expand automatically. Remote password
administrators can manage only grants within their own scope authority and expiry.
They can delegate the dynamic selector only if they hold it. Public access does
not expand their delegation rights. Issued records are independent; revoking a
creator does not revoke its issued grants.

Active passwords must be unique. An expired/revoked secret may be reused in a new
record with new authority. Anyone who remembers that secret can explicitly log
in under the new grant; old sessions never transfer to it. Expiry extension cannot
create a collision with an existing valid record.

Sessions use HttpOnly/SameSite=Strict cookies. Their default absolute maximum is
seven days, idle limit 24 hours, capped by password expiry. Polling and denied calls
do not update last use or idle activity. Revocation/expiry blocks the next protected
operation, including queued management work before it starts. Already admitted
business roots retain their authority and finish even if the browser disconnects.
WebSockets recheck at one-second intervals; this cleanup is separate from per-call
admission. Healthy reload/restart retains eligible sessions, not Streamlit memory.

## Failure and recovery

An active auth provider or Dashboard Auth integration failure/disablement selects
**public business access**, with its reason in the common header. System operations
remain controlled. Recovery resumes saved policy without changing TOML. Normal
startup/reload of a protected chain returns retryable unavailability; it does not
briefly open access. Invalid passwords, expiry, denied scopes, malformed requests
and rate limits are denials, not failures. A rejected candidate leaves a healthy
active generation's access mode intact.

The existing management token at `APP/.toolframe/management.token` remains an
independent recovery credential for CLI/API and `toolframe open`. Direct socket
loopback peers obtain local control by default; connecting to the machine's LAN
address is remote. Host/Origin checks remain enforced and forwarded headers are
ignored. This release covers direct localhost/LAN HTTP; it does not implement a
trusted reverse-proxy mode or TLS termination.

Back up the composition, `data/plugins/auth/data.sqlite` (using a consistent SQLite
backup) and `.toolframe/secrets/auth/password-lookup.key` together. Core creates the
key as mode 0600 and retains it on removal/reload. It is separate from the auth
database, management token and config. A missing key alongside an existing store
fails activation; restore the original key rather than generating a replacement.
Scratch runtime validation uses a separate temporary store/key. Password/session
secrets never enter management operation history or exported composition.

## SDK and existing applications

All plugins now require `AuthorizationSpec`, explicit method `audience`, method
scopes for business/system APIs and page scopes. Legacy packages fail validation;
there is no synthetic whole-plugin permission. Update custom definitions using
the bundled skill's `references/plugin-api.md` and SDK 0.2. Update shipped provider
packages to 0.2.0 and the demo to 0.2.1, then apply the reviewed composition together.
The auth plugins are at 0.1.1. Keep the old executable/composition for rollback
until the new packages validate; ordinary business databases keep their schemas.

An upgraded composition without `[access]` adopts public business access once its
compatible plugins activate. It does not acquire auth plugins automatically.
Add them explicitly to enable password management. The bundled skill is versioned
in SKILL.md frontmatter; normal clean-skill upgrades and customized-skill prompts
still apply. Generated `plugins/` business source is never overwritten.

After the existing providers have been upgraded to 0.2.0 and core is running:

```sh
toolframe --home APP plugin add pypi:toolframe-auth==0.1.1 --enable
toolframe --home APP plugin add pypi:toolframe-dashboard-auth==0.1.1 --enable
```

The standard binary supplies these wheels. Open **Access passwords** through the
server's loopback address to configure grants. This explicit installation is
separate from replacing an application's executable.

## Protected auth API

`POST /_toolframe/auth/login` accepts `{ "password": "..." }`, with
`Accept: application/json` for JSON success and an HttpOnly cookie. `POST
/_toolframe/auth/logout` ends that browser's session. `GET
/_toolframe/auth/context` gives safe mode/permission presentation data.

`POST /_toolframe/v1/auth/ACTION` accepts object JSON for `catalog`, `list`,
`create`, `update`, `revoke` and `policy`. Catalog supplies revision and scopes.
Create takes `label`, optional `password` (omit/null to generate), `scopes`,
`all_business`, optional Unix `expires_at` and `expected_revision`. Update takes
`id` plus all grant fields; revoke takes `id`. Policy takes `mode`, `local_control`
and `expected_revision`. These calls enforce the same rules as Dashboard.
