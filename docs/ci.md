# CI checks

Ordinary CI validates Python distributions, PostgreSQL integration and four native
launcher wheels. Publication is a separate manual workflow described in
[releasing](releasing.md).

## Validation workflows

`ci.yml` runs on pull request source updates, pushes to `main`, and manual
dispatch. Feature branch pushes do not start a second full validation alongside
the PR. Before opening a PR, use manual dispatch when remote validation is needed.
Release tags are validated by `publish.yml`.

Branch policy, lint, formatting and applicable PR commit/title checks must pass
in `quality.yml` before `validate.yml` can start. Publication applies the same
quality gate after checking its release tag. Python integration and architecture
tests run on macOS ARM64. The PostgreSQL job starts its own PostgreSQL 16 service
and exercises Core journals, failure recovery and simultaneous SQLite/PostgreSQL
workers.
`launcher.yml` builds and exercises native wheels on macOS ARM64, macOS Intel,
Linux ARM64 and Linux x86-64. Linux wheels use manylinux 2.28 containers. Each job
has a fresh workspace and uploads only its own validated artifacts.

The final `CI result` check succeeds only when both quality and validation
succeed; failed, cancelled or skipped prerequisites cannot produce a green
result. `pr-policy.yml` also provides a standalone `PR policy` check on PR source
updates and metadata edits. This small policy check runs both independently and
inside the source-validation gate, so a title edit can update a required check
without cancelling or repeating native builds. Configure both `CI result` and
the standalone `PR policy` as required status checks; required checks are a
repository setting, not enabled by these workflow files.

Dependabot batches Docker and GitHub Actions updates into separate version and
security groups. Catch-all groups include major versions and use compliant
`chore/<group-name>` branches. To split a dependency out, add a compliant named
group before the catch-all; exclusions would restore individual branch names.

Python 3.14.7, uv 0.12.8, Rust 1.94.1 and Maturin 1.14.1 are the build pins.
Third-party actions use full commit IDs. Runtime uv compatibility is a separate
contract in [runtime preparation](runtime.md).

## Coordinated source dependencies

[`scripts/ci/dependencies.json`](../scripts/ci/dependencies.json) pins the SDK,
Cordis and the providers needed by integration tests. CI fetches each exact
commit from the trusted owner in `TOOLFRAME_REPOSITORY_OWNER`, defaulting to
`github.repository_owner`. For private siblings, supply a read-only
`TOOLFRAME_READ_TOKEN` secret with access to those repositories. The ordinary
repository token cannot read private siblings; fork PRs also do not receive
repository secrets. Public repositories need no extra token.

Local validation uses the pinned Git objects from sibling checkouts. It archives
those revisions into a temporary workspace, preserving the original working
trees. Core's current tracked and nonignored new files are copied there. Ignored
caches, environments, artifacts and local notes are excluded. The source root
can be selected explicitly for worktree development.

```sh
python3.14 scripts/ci/validate.py --checks python \
  --source-root /path/to/sibling-repositories --output build/check-python
python3.14 scripts/ci/validate.py --checks launcher \
  --target aarch64-apple-darwin --source-root /path/to/sibling-repositories \
  --output build/check-launcher
```

Each output directory must be new. `--repository-owner OWNER` uses GitHub instead
of local Git objects. Validation records the exact dependency pins, environment,
results and temporary workspace path. Keep historical evidence in CI artifacts
or ignored local directories.

## Artifacts and consumer checks

Python validation runs Ruff, the full pytest suite, wheel/sdist builds with
`--no-sources`, strict Twine checks, license verification and fresh wheel
installation with real dependencies. Skill evaluation inputs must stay outside
the installed Core wheel; the authoring skill, templates and licenses remain
shipped product assets.

Native validation runs Rust formatting and tests. `prepare-launcher.py` creates
a separate wheelhouse for prepublication application tests. It never puts those
wheels in the native build tree. Intel builds fetch the
[pinned cryptography companion](intel-companion.md), verifying its SHA-256, size,
distribution metadata and platform/ABI tag. Ordinary CI and publication never
compile this dependency or fall back to a source build when a download fails.
The verified bytes are copied into each Core Release as a separate asset; only
that release's URL, filename and hash enter Rust.

Maturin produces a platform wheel containing a native `toolframe` command without
a Python wrapper. `build-launcher.py finish` exports the identical executable,
checks its layout, and writes its release archive, checksums and manifest.
`check-launcher-wheel.py` installs it through `uv tool install` and `uvx`, compares
executable hashes, then runs release and skill acceptance outside the checkout.
These probes exercise the empty host, complete template and provider startup,
llms responses, retained data, plugin reload, warm-cache offline restart and
missing-cache errors. Browser interactions and independent MCP calls are separate
acceptance checks.

Before publication those tests use the explicit validation wheelhouse. After
publication repeat the consumer check without `--wheelhouse` to verify public
PyPI and companion Release downloads. Passing local wheel tests does not establish
that remote uploads or index resolution have succeeded.

Artifacts include Core's wheel/sdist, four native wheels, four executable archives,
release manifests/checksums, the Intel companion and JSON/XML diagnostics. Actions
retains them for 14 days. Binaries are unsigned and are not Apple-notarized.
