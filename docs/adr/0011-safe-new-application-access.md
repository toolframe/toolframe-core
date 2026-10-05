# Safe access defaults for new applications

New applications write `access.failure_mode="closed"`. A local or packaged template
inherits this value only if it omits the field. This preserves a protected
application's credential boundary during authentication failure or disablement;
the tradeoff is temporary unavailability of protected business operations.
Intentional public mode remains public. This changes the new-application default
from [ADR 0003](0003-public-fallback-on-auth-error-or-disablement.md).

Loading an older composition without the field keeps its previous public fallback,
so an upgrade cannot silently change an existing application's recovery behavior.
Explicit template and application choices are preserved. Selecting protected mode
from public or creating the first password supplies closed policy when absent.
Saving an already-protected legacy policy without an explicit failure choice
keeps its existing behavior.

The operator can select either failure policy through a revision-checked API and
Dashboard control. The catalog advertises this capability so an older Core cannot
silently ignore a control offered by a newer page. The management page also reports
Dashboard, independent MCP, public HTTP and local-control boundaries. A public
HTTP declaration does not imply an active plugin; scopes authorize methods and do
not implement identity or row isolation.

These changes do not couple MCP credentials to Dashboard passwords. Protected MCP
keeps its own closed admission rule; explicit public HTTP and llms publications
remain public. See [authentication](../authentication.md) for configuration,
entrance rules and recovery.
