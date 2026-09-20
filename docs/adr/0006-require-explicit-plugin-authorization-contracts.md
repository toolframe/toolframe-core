# Require scope declarations instead of synthesizing legacy permissions

Business plugins must declare explicit scopes before activation. A compatibility
fallback granting whole-plugin permission would infer authority over an unknown
service surface, so legacy plugins require an explicit update. Entry Register and the bundled
`plugin new` template must demonstrate explicit read/write scopes, and shipped
providers must state their relevant system/internal access contracts.

This avoids turning an unknown service surface into an inferred business grant.
Publishing a business API remains an additional explicit choice, not a side effect
of declaring a scope. Validation and the upgrade guide must explain missing
contracts before rollout; a new binary alone does not make an old package valid.
