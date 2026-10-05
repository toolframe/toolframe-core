# Open Dashboard access when auth errors or is disabled

Partially superseded by [ADR 0011](0011-safe-new-application-access.md): new
applications select closed failure policy, while legacy and explicit public
fallback retain the behavior below.

Dashboard access becomes public when the auth plugin errors or is disabled,
with a persistent header notice that the application
is publicly accessible. This deliberately favors continued application access
over maintaining the Dashboard credential gate during an auth outage. A fail-closed
policy would preserve that gate but make protected business access unavailable
until authentication recovers.

This is distinct from invalid, expired or revoked passwords: those are normal
access decisions and do not trigger fallback. The public-access notice must be
available independently of the failed auth plugin. System API authorization and
the local operator recovery boundary still apply under the separately accepted
access rules.

Fallback changes only Effective Access Mode:
it preserves Configured Access Policy and automatically restores that policy when
auth recovers. Anonymous visitors receive all business scopes, while system
authority still requires Local Control, the management token, or a currently
verifiable password with the appropriate system scopes. The header distinguishes
intentional public access from public fallback.

Normal startup/reload is a separate transient: new protected operations wait
until auth is ready, rather than temporarily becoming public. An actual failure
or explicit disablement activates fallback. Transition deadlines and request-error
classification are described in [authentication](../authentication.md).

[ADR 0008](0008-shared-oidc-authentication.md) adds an explicit `closed` failure
mode for protected deployments. The behavior above remains the compatibility
default when that option is not selected.
