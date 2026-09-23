# CI and release preparation

CI validates the Python distribution and the supported macOS standard launcher.
It prepares downloadable workflow artifacts; it does not create a GitHub Release,
sign or notarize an executable, or upload Python packages during ordinary CI.

## Workflows

- `ci.yml` runs on pull requests, pushes (including tags), and manual dispatches.
  It calls `validate.yml` with read-only repository permissions.
- `validate.yml` runs the complete Python integration/architecture suite on
  `macos-15` (arm64), and Rust checks plus standard-bundle acceptance on
  `macos-15` and `macos-15-intel`. Each job has a fresh workspace and a timeout.
  Reusable validation deliberately has no concurrency group; it cannot cancel
  its caller. Ordinary CI cancels only superseded runs of its own ref.
- `publish.yml` is manual only. Select the `v<project.version>` tag as the run
  ref and choose `testpypi` (default) or `pypi`. Branches, mismatched tags, and
  mismatched Cargo/Python versions fail before validation. The same revision is
  validated again, including both macOS launcher jobs. Only then does the
  publishing job download that run's `python-dist` artifact and upload it using
  the PyPA action and the chosen environment's Trusted Publisher. Only that job
  receives `id-token: write`; no API-token secret is required.

All third-party actions are pinned to full commits with release comments. The
runner labels were checked against [GitHub's runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
Python 3.14.7 and uv 0.12.8 use the official setup actions described by
[uv's GitHub Actions guide](https://docs.astral.sh/uv/guides/integration/github/).
Rust 1.94.1 is installed with rustup and includes rustfmt.

## Pinned sibling sources

The eleven required siblings and full commit IDs are in
[`scripts/ci/dependencies.json`](../scripts/ci/dependencies.json). These are the
SDK, Cordis, eight providers, and Entry Register. MCP Auth and OIDC Auth
are fetched for coordinated authentication tests; they are not automatically
added to the standard launcher bundle. Core's architecture tests inspect
their sources; integration tests need a newly built SDK wheel at the expected
`toolframe-sdk/dist` path; standard launcher builds embed their wheels.

GitHub jobs fetch these exact commits from
`https://github.com/<owner>/<repository>.git`. The owner defaults to
`github.repository_owner`, which is the base repository owner for a fork PR.
Set the repository variable `TOOLFRAME_REPOSITORY_OWNER` if the siblings live
under another trusted owner. The workflow never derives that owner from the PR
contributor. Push every named repository and pinned commit to that owner before
running remote CI. The current fetch path requires public/readable repositories;
the default repository `GITHUB_TOKEN` does not grant access to private siblings.
Private siblings need a separately designed read-only access arrangement before
CI can run, particularly for fork PRs.

Locally, the validator reads those commits from sibling Git object databases and
extracts them with `git archive`. It never builds, syncs, modifies lockfiles, or
writes artifacts in the source siblings. Core is copied from its current tracked
files plus nonignored new files, so local changes can be checked before commit.
Ignored `.venv`, `dist`, `wheelhouse`, `target`, and caches are never copied.

## Repeat the same checks locally

Install Python 3.14.7, uv 0.12.8, Git, Rust 1.94.1 with rustfmt, and the Xcode
command-line tools. Run from the core checkout, with the pinned sibling commits
available in repositories under its parent directory:

```sh
python3.14 scripts/ci/validate.py --checks python --output build/ci-python
python3.14 scripts/ci/validate.py --checks launcher \
  --target aarch64-apple-darwin --output build/ci-arm64
python3.14 scripts/ci/validate.py --checks launcher \
  --target x86_64-apple-darwin --output build/ci-intel
```

Each `--output` directory must be new. Use a new suffix for a repeat run. Add
`--source-root /path/to/siblings` for another local source directory, or
`--repository-owner OWNER` to exercise the same public GitHub fetch path as CI.
Each invocation creates an independent temporary workspace, new uv cache and
Python installation directory, new virtual environments, empty dependency wheel
directories, and a new Cargo target directory. Downloaded Rust registry sources
may already exist in rustup/Cargo's normal cache. Cargo builds use two parallel
jobs (`CARGO_BUILD_JOBS=2`) to bound CPU/memory use. The workspace is retained for
debugging and its location is written to `workspace.txt`; remove that temporary
directory after examining the evidence if it is no longer needed.

Python validation runs `uv sync --locked --group dev`, the existing Ruff scope
plus the CI helpers, and the full `pytest -q` suite without exclusions. It builds
SDK/Cordis dependency wheels from the pinned sources, builds core's sdist and
wheel with `uv build --no-sources`, runs `twine check --strict` with Twine 7.0.0
under the explicit Python 3.14.7 interpreter,
checks all declared license files in both archives and exclusion of skill evals
from the wheel, then installs the wheel plus the real SDK/Cordis wheels and
public runtime dependencies into a separate fresh environment. `uv pip check`,
isolated imports from site-packages, and the installed CLI's help command must
all pass. No `--no-deps` install is used.

Launcher validation runs `cargo fmt --check`, `cargo check --locked --all-targets`,
and `cargo test --locked`, then the existing `scripts/build-release.sh` with
`--locked`. The Intel route first runs the pinned FastMCP
`scripts/build-macos-intel.py`: this checks the OpenSSL 4.0.2 and cryptography
50.0.1 source hashes and builds the required
`cryptography-50.0.1-cp314-abi3-macosx_12_0_x86_64.whl` in a new wheelhouse.
It requires make, Perl, clang, and the x86_64 Rust target; Apple Silicon also
requires Rosetta. Missing native requirements fail the check.

Both architectures must pass the existing `verify-release.py` and
`verify-skill.py` against a copied executable outside the source checkout.
Release acceptance checks empty and Entry Register applications, online startup,
warm-cache offline restart, persistent data, plugin reload, isolated runtimes,
and rejection of an offline cold start. Its first run has fresh uv/Python caches
and receives no external wheelhouse. Skill acceptance covers installation,
updates, customized-file preservation, and an independently buildable starter.

Artifacts include core's wheel/sdist, pytest XML, JSON evidence, and a launcher
`.tar.gz` with executable permissions, license notices, and a `SHA256SUMS` file.
Only successful checks upload the distribution/launcher artifacts; diagnostic
JSON/XML is also retained on failures. Actions artifacts expire after 14 days.

Static checks and the publication gate can be repeated separately:

```sh
actionlint
bash -n scripts/build-release.sh scripts/dev-wheelhouse.sh
python3.14 scripts/ci/check-release.py --ref refs/tags/v0.5.3
```

## External publication setup

1. Push core and all required sibling commits to the chosen GitHub owner and
   enable Actions. Set `TOOLFRAME_REPOSITORY_OWNER` only when the default is wrong.
2. Create GitHub environments named exactly `testpypi` and `pypi`. Configure
   required reviewers and tag protection appropriate to the project, and permit
   only release tags to enter the publishing environments.
3. On **each** of TestPyPI and PyPI, configure a normal or pending Trusted
   Publisher for project `toolframe-core`, the actual GitHub owner and repository
   `toolframe-core`, workflow filename `publish.yml`, and matching environment
   (`testpypi` or `pypi`). Follow the
   [PyPI Trusted Publishing instructions](https://docs.pypi.org/trusted-publishers/using-a-publisher/).
4. Ensure `toolframe-sdk==0.3.0` and `fastapi-cordis==0.2.0` are available to
   consumers on the intended package index. Local pinned wheels prove the
   distribution's installation; they do not publish those dependency packages.
   TestPyPI may not contain every public dependency, so a TestPyPI install needs
   an explicit, carefully selected dependency source.
5. Push the matching version tag (currently `v0.5.3`) and manually run
   **Publish Python distribution** at that tag, initially with `testpypi`.
   Use a tag containing these workflow files. A failed/repeated upload is not
   silently ignored; package indexes do not permit replacing published files.

No GitHub repository, publisher, environment, tag, release, or account setting is
created by the local validator. Unsigned launcher artifacts are preparation for
release review, not evidence of Apple signing/notarization or distribution on all
supported machines. Native arm64, Intel under Rosetta, physical Intel, macOS 12,
and GitHub-hosted execution must be reported separately. Linux/Windows application
support is outside this launcher route. See the [testing guide](testing.md) for integration and release checks.
