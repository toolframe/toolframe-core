# 0016: Preserve public plugin contracts across 0.x releases

## Context

Core installs plugins in isolated workers while selecting the SDK used by each
worker. Exact SDK patch requirements and minor-version authoring ceilings can
reject an otherwise compatible plugin when Core changes its selected SDK or
minor version. Requiring every independent plugin to release for that change
couples package versions without describing an actual API requirement.

## Decision

Starting with SDK 0.4 and Core 0.8, preserve documented public plugin APIs,
worker protocol behavior, authoring contracts and the public maintenance API
through supported 0.x releases. The existing Core 0.7 maintenance API remains
the minimum required by Backup. Additive features may require a newer minimum;
deprecated public interfaces remain usable. Breaking changes require a
compatibility implementation or an explicitly announced migration boundary.
Private and explicitly experimental interfaces are outside this promise.

Independent plugins declare the lowest verified SDK and an upper bound of `<1`.
Web contracts require at least SDK 0.4.1. Official authoring manifests declare
Core `>=0.8,<1`; explicit third-party bounds remain enforced. Core may pin its own
SDK, and deployment locks continue to record exact versions and artifact hashes.

The [SDK compatibility policy](https://github.com/toolframe/toolframe-sdk/blob/main/docs/compatibility.md)
is authoritative for plugin dependency declarations and SDK qualification.
Core's architecture and authoring guides describe its runtime and authoring gates.

## Consequences

Compatible Core/SDK updates can reuse an existing plugin distribution. Published
wheel metadata is immutable, so existing exact pins require one new distribution
before this policy can benefit that plugin. The package version can remain 0.x.

The wider range is a project commitment, not a guarantee supplied by semantic
versioning or proof that future versions were tested. Minimum/latest SDK CI,
retained-wheel qualification and Backup's minimum/latest Core operator checks
make regressions observable. Resolving an incompatible dependency may require a
fix or a targeted documented exclusion. Authorization, protocol validation,
configuration checks and application ownership checks stay in force.

We reject moving all packages to one shared version: it would retain release
coupling. We also reject removing all upper bounds: a future 1.0 contract needs
an explicit compatibility decision.
