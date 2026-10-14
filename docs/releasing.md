# Releasing Toolframe

Toolframe has two installation channels containing the same Rust executable:
platform wheels for the PyPI project `toolframe`, and executable archives in
[GitHub Releases](https://github.com/toolframe/toolframe-core/releases).
`toolframe-core` remains the separate Python runtime distribution. The launcher
contains no Python wheels; it resolves Core and selected plugins from PyPI.

## Coordinated first release

Publish SDK 0.3.0 first. Cordis 0.2.2 must be available before Core. Then publish
the template packages: storage-sqlite 0.3.1, auth 0.2.0, dashboard-auth 0.1.3,
streamlit-dashboard 0.4.2, fastmcp 0.2.1, llms-txt 0.2.1 and
demo-entry-register 0.3.1. Each repository owns its own tag, validation and
`publish.yml`. Core's source-validation pins must point to the coordinated
commits available under the trusted GitHub owner.

Publish `toolframe-core==0.6.1` and `toolframe==0.6.1` together through Core's
workflow after those packages are available. Optional packages can follow:
object-store-local 0.1.0, object-store-s3 0.1.0, mcp-auth 0.2.0, oidc-auth 0.2.0,
storage-postgres 0.1.0, jobs 0.1.0, audit 0.1.0, backup 0.1.0 and testkit 0.1.0.
Backup's operator extra requires Core. Testkit's pytest integration is an extra.

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
launcher wheels on four platforms. Publication is manual: select tag `v0.6.1`
for `publish.yml`, choose `testpypi`, inspect that run, then select `pypi`.
The tag must match both Python and Cargo versions. Branches and mismatched tags
fail before validation.

The workflow reruns validation at the tag, downloads exactly one Core wheel,
one Core sdist and four native wheels from that run, then uploads each project's
files in its own job. Both production uploads must succeed before publishing
GitHub Release assets: four
executable archives, manifests, checksums and the Intel macOS cryptography wheel.
The companion is built from verified upstream sources. Its payload stays separate;
only its exact URL and SHA-256 are compiled into the Intel launcher. Do not replace
a published companion with different bytes: existing launchers will reject it.

PyPI files are immutable. If upload partially succeeds, inspect the accepted
files before recovery; do not rebuild and blindly retry the same version.
If only GitHub Release creation fails, recover it from the validated
`launcher-release-*` artifacts of the same run and verify their checksums before
publishing. Do not rerun package publication solely to repair release assets.

The CLI binaries are unsigned and not Apple-notarized. macOS 12+, Linux glibc
2.28+, both CPU architectures, physical hardware and any emulation used for
checks must be identified in release evidence. Windows and musl Linux are not
included.

## Verify public installations

Prepublication acceptance supplies matching local wheels explicitly. After all
uploads and repository visibility changes, run in a fresh environment:

```sh
uv tool install toolframe==0.6.1
toolframe --version
toolframe --home ./release-check init --template entry-register
toolframe --home ./release-check serve
# In another terminal:
toolframe --home ./release-check open
```

Repeat with `uvx --from toolframe==0.6.1 toolframe ...` and with the downloaded
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
