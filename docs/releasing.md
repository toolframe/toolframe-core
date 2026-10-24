# Releasing Toolframe

Toolframe has two installation channels containing the same Rust executable:
platform wheels for the PyPI project `toolframe`, and executable archives in
[GitHub Releases](https://github.com/toolframe/toolframe-core/releases).
`toolframe-core` remains the separate Python runtime distribution. The launcher
contains no Python wheels; it resolves Core and selected plugins from PyPI.

## Coordinated release

Publish SDK 0.4.1 first. Core requires Cordis 0.2.2. The Core 0.8 application
combination uses SQLite 0.4.1, auth 0.3.1, dashboard-auth 0.2.1, Dashboard 0.5.1,
Web 0.1.0, FastMCP 0.3.0, llms-txt 0.3.0 and Entry Register 0.4.1. Optional
PostgreSQL 0.2.1, MCP Auth 0.3.1 and OIDC Auth 0.3.1 accept compatible SDK 0.4
patches. Backup 0.2.1 supports the Core 0.8 operator extra and gateway mounts.

Each repository owns its tag, validation and `publish.yml`. Publish dependencies
before Core, update the CI source pins to the verified release commits, and pin
the public Web sdist by hash. The Web repository may remain private; its PyPI
wheel and sdist are public. See [CI](ci.md) for source acquisition and local
prepublication artifacts.

Publish `toolframe-core==0.8.0` and `toolframe==0.8.0` together through Core's
workflow after the dependency combination is available. Core and launcher versions
must match. The launcher resolves the package versions selected by the template;
its native wheel does not bundle Python distributions.

## Publisher setup

Create GitHub environments `testpypi` and `pypi` for Core, and
`testpypi-launcher` and `pypi-launcher` for the launcher. On each corresponding
index, create a Trusted Publisher for **both** projects below:

| PyPI project | GitHub owner | Repository | Workflow | Environment |
| --- | --- | --- | --- | --- |
| `toolframe-core` | `toolframe` | `toolframe-core` | `publish.yml` | `testpypi` or `pypi` |
| `toolframe` | `toolframe` | `toolframe-core` | `publish.yml` | `testpypi-launcher` or `pypi-launcher` |

The projects use separate upload jobs and environments so that each has a
distinct publisher identity. PyPI does not allow two pending publishers with the
same repository, workflow and environment for different project names.

Use the actual owner if publishing from a fork. Each provider uses the same
workflow/environment names in its own repository; see that repository's release
guide. Follow [PyPI's Trusted Publishing documentation](https://docs.pypi.org/trusted-publishers/using-a-publisher/).
Only the upload jobs receive `id-token: write`; they use no PyPI API-token secret.
Environment reviewers and release-tag protections can restrict who releases.

Source validation fetches sibling repositories. Public repositories need no
additional credentials. While they are private, the optional read-only
`TOOLFRAME_READ_TOKEN` secret must cover those repositories. Fork PRs do not
receive repository secrets. Local validation remains available without remote
credentials; see [CI](ci.md).

## Validate and publish

`ci.yml` validates Core's wheel/sdist, real PostgreSQL integration and native
launcher wheels on four platforms. Publication is manual: select tag `v0.8.0`
for `publish.yml`, choose `testpypi`, inspect that run, then select `pypi`.
The tag must match both Python and Cargo versions. Branches and mismatched tags
fail before validation.

The workflow promotes the successful main CI bundle for the exact tag commit,
verifying its source tree, versions, provenance and all file hashes. When no
eligible bundle is available it runs complete validation at the tag; the manual
`rebuild` input also forces fresh validation. The resulting `validated-release`
bundle belongs to this publication run. Each upload job verifies it again and
stages exactly one Core wheel and sdist, or all four native wheels. See the
[artifact contract](artifacts.md) for the PR → main → release promotion rules.
Both production uploads must succeed before publishing
GitHub Release assets: four
executable archives, manifests, checksums and the Intel macOS cryptography wheel.
The companion reuses the verified bytes from the fixed Toolframe Release asset
in the [Intel companion manifest](intel-companion.md). Publication does not
compile cryptography or OpenSSL. It copies the same wheel into the new Core
Release, preserving the launcher URL for that Core version. Its payload stays
separate; only its exact URL and SHA-256 are compiled into the Intel launcher.
Keep the source Release asset available for subsequent CI runs, and do not
replace a published companion with different bytes: existing launchers will
reject it. Native dependency updates use the explicit build procedure in the
companion guide before ordinary CI or publication.

After production Release assets succeed, `publish-containers` calls the
[container workflow](container.md#publish-to-github-container-registry). It builds
and verifies Debian and Distroless images on native Linux ARM64 and x86-64 using
the validated executable artifacts from the same run, then publishes their GHCR
tags after the [container security policy](container.md#security-checks-and-updates)
passes. First publication also requires setting the GHCR package to Public. A
container-only rebuild or failed container publication can be retried through
the separate Container images workflow without re-uploading Python packages.

PyPI files are immutable. If upload partially succeeds, inspect the accepted
files before recovery; do not rebuild and blindly retry the same version.
If only GitHub Release creation fails, recover it from the `launcher-release-*`
directories inside that run's `validated-release` bundle and verify their checksums
before publishing. Do not rerun package publication solely to repair release assets.

The CLI binaries are unsigned and not Apple-notarized. macOS 12+, Linux glibc
2.28+, both CPU architectures, physical hardware and any emulation used for
checks must be identified in release evidence. Windows and musl Linux are not
included.

## Verify public installations

Prepublication acceptance supplies matching local wheels explicitly. After all
uploads, run in a fresh environment:

```sh
uv tool install toolframe==0.8.0
toolframe --version
toolframe --home ./release-check init --template entry-register
toolframe --home ./release-check serve
# In another terminal:
toolframe --home ./release-check open
```

Repeat with `uvx --from toolframe==0.8.0 toolframe ...` and with the downloaded
native archive. Compare the archive SHA-256 with `SHA256SUMS`. The automated
consumer probe can be repeated without a test wheelhouse:

```sh
python3.14 scripts/verify-release.py --binary /path/to/toolframe \
  --architecture arm64 --output build/public-release.json
python3.14 scripts/verify-skill.py --binary /path/to/toolframe \
  --output build/public-skill.json
```

Verify Dashboard and an independent MCP client, then restart with `--offline`
and confirm retained data. On Intel macOS include an empty companion cache to
exercise the actual Release download. TestPyPI does not mirror public
dependencies; use explicit dependency sources for staging checks. Record actual
commands, revisions, hashes and results in CI artifacts or ignored local notes.
