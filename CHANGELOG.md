# Changelog

These versions describe local development releases. A version entry does not
imply that its distributions have been published or every target is qualified.

## Unreleased

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
