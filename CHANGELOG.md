# Changelog

These versions describe local development releases. A version entry does not
imply that its distributions have been published or every target is qualified.

## Unreleased

## 0.9.0

- Resolve named application recipes to the latest compatible stable, non-yanked
  plugin releases within their declared minimum and upper bounds. Backtrack over
  incompatible metadata while preserving Core's SDK and backend isolation.
- Freeze exact sources, artifact hashes and environment locks before first
  serve, retaining recipe ranges in the existing candidate provenance for
  explicit updates. Dashboard and Web extras use their existing shared cohorts.
- Retain selected index versions through serve, restart, validate and reload;
  only explicit update selects again. Restore missing environments from their
  saved locks and reject missing or modified captured artifacts.
- Use the Entry Register 0.4.2 recipe and remove the per-plugin
  `toolframe-fastmcp` patch override. Preserve explicit pins and custom sources,
  support verified offline resources, and leave failed initialization retryable.

## 0.8.1

- Redact schema-declared secrets from the composition snapshot being exported,
  including manually rotated values before refresh.
- Set Secure authentication cookies using the parsed HTTPS scheme, including
  uppercase and mixed-case HTTPS origins.
- Prevent Dashboard and management pages from being framed while retaining
  upstream Content Security Policy directives.
- Reject new and waiting managed task admission during shutdown so worker
  disposal can settle SDK ownership handshakes promptly.
- Accept valid Linux kernel process groups with ID zero during stopped-state
  inspection while preserving ownership scans and malformed-inventory refusals.
- Select FastMCP 0.3.1 when initializing the named Entry Register recipe;
  existing applications and explicit local recipes keep their source selections.
- Preserve documented public plugin and authoring contracts across Core 0.x,
  and require retained-plugin wheel verification in release bundles.

## 0.7.0

- Rename the SQL providers to `toolframe-sql-sqlite` and `toolframe-sql-postgres`,
  with plugin IDs `sql-sqlite` and `sql-postgres`.
- Adopt SDK 0.4's `RuntimeContext.sql()`, `SqlClient`, and `toolframe.sql` service
  contract throughout Core, templates, and plugin documentation. Install the
  matching SDK 0.4-compatible plugin releases together.

## 0.6.4

- Refresh the container runtime packages to include Debian
  `libexpat1 2.9.0-1~deb13u1`, fixing six Expat security advisories in
  the standard image. Both image variants retain the existing vulnerability
  publication policy.

## 0.6.3

- Apply container security updates, remove unused pip bootstrap dependencies and
  privileged executable bits, and gate publication on vulnerability scans with
  daily image checks and weekly dependency update proposals.

## 0.6.2

- Add Debian and Distroless container images with prepared Core runtimes, Tini,
  persistent application volumes and verified multi-platform GHCR publication.
- Run multiple SQL storage providers using service defaults and per-plugin bindings.
- Retain store provider ownership across removal and restart; reject silent rebinding.
- Bind migration plans and forward repair to their provider, and preserve admitted
  bindings and cleanup across storage cohorts.

## 0.5.2

- Exclude development evaluation fixtures from installed plugin skills and bundles.
- Preserve customized skills while upgrading unchanged installed copies.

## 0.5.1

- Align the Dashboard operation-history refresh and pagination controls.

## 0.5.0

- Add permanent operation sequence numbers and paginated, filtered history.
- Upgrade existing journal rows while retaining their operation UUIDs.

## 0.4.1

- Reuse compatible retained plugin artifacts after executable upgrades.
- Guide disabled plugins through enable operations and show Dashboard loading state.

## 0.4.0

- Add dependency, binding and presentation diagnostics with operation impact views.

## 0.3.1

- Simplify password creation, relative expiry and local timestamp presentation.

## 0.3.0

- Add independent Auth and Dashboard Auth plugins and explicit SDK authorization.
- Separate configured Dashboard access policy from effective fallback behavior.

## 0.2.0

- Add configurable localhost/LAN listeners and standard executable bundles.
- Report launcher versions without runtime preparation.
