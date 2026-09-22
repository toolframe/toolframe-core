# Protect MCP with independently managed bearer keys

MCP authentication uses 32 random bytes (256 bits) for clients supporting bearer
tokens or custom Authorization headers. The first version supports manually
configured credentials; OAuth discovery and browser sign-in are deferred.

The `mcp-auth` provider owns credential digests and grants. Core owns the independent
`mcp_access` policy, validates every external MCP request and checks resource and
method scopes before tool admission. FastMCP receives a temporary core request
credential rather than the external bearer secret. Dashboard Auth presents key
management through protected core APIs.

Existing applications retain public MCP until explicitly protected. Protected MCP
stays closed during auth failure, reload, disablement and zero-key states. This
new MCP policy is separate from Dashboard public fallback.
Local MCP clients require keys too. Key creation never changes policy implicitly.

Keys have fixed business scopes and optional expiry. Administrators cannot delegate
beyond their own scopes/expiry. Issued keys remain independent of their creator.
Revocation blocks subsequent requests and tool admissions; already admitted roots
retain their authority and finish. Stateful MCP sessions bind to the key record.
