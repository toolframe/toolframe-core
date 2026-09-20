# Keep public business entrances separate from scoped Dashboard access

Dashboard passwords use named per-operation scopes. MCP, llms.txt and explicitly
published business APIs remain public. Scope
checks apply to protected Dashboard operations; they do not restrict the same
operation when it is deliberately published through a public entrance. System
APIs and internal services are not made public by this choice.

System scopes cover plugin administration, migrations, application settings and
password management. They are excluded by default but can be explicitly granted
for remote management. "All business scopes" is a dynamic grant that includes
future business scopes for existing passwords, rather than a creation-time
snapshot; this supports access to newly added business plugins without editing
every password. Individually selected scopes remain explicit, and neither form
implicitly grants system privileges. Publishing an API/tool and classifying a
scope as business are therefore meaningful access decisions by plugin authors
and application operators.

A remote password administrator may manage only records within its own scope
authority and cannot issue or extend a credential past its own expiry. Issued
records are independent; revoking their creator does not cascade. Local Control
and the existing management token can grant all scopes. Anonymous visitors
receive all business scopes in both intentional public mode and public fallback;
neither grants system authority. Password-authenticated system administration
requires a functioning auth verifier even during public fallback.
