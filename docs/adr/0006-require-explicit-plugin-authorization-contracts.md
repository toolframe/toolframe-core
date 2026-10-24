# Require scope declarations instead of synthesizing legacy permissions

Business plugins must declare explicit scopes before activation. A compatibility
fallback granting whole-plugin permission would infer authority over an unknown
service surface, so legacy plugins require an explicit update. Entry Register and
business plugin examples demonstrate explicit read/write scopes, and shipped
providers must state their relevant system/internal access contracts.

The original requirement that the default bundled `plugin new` template must
demonstrate read/write scopes is superseded by
[distribution-owned authoring](0015-distribution-authoring.md). The default
`toolframe-core/basic` template has an internal echo service and an explicit
`AuthorizationSpec(scopes={})`. Optional business examples demonstrate read/write
scopes. This changes the default example, not the requirement for business plugins
to declare their authorization contracts explicitly.

This avoids turning an unknown service surface into an inferred business grant.
Publishing a business API remains an additional explicit choice, not a side effect
of declaring a scope. Validation and the upgrade guide must explain missing
contracts before rollout; a new binary alone does not make an old package valid.
