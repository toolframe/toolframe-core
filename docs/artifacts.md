# Validated build artifacts

Core CI produces one `validated-release` bundle containing the Core wheel and
sdist, four native launcher wheels, four executable archives, the Intel companion,
release manifests/checksums and Python, PostgreSQL and installed-launcher evidence.
The bundle is an Actions artifact in the existing Core repository. It requires
no separate repository, package registry or service.

## Promotion rules

| Consumer | Eligible validation source |
| --- | --- |
| PR source update | Complete fresh validation |
| Main CI | Successful CI for this exact commit, or the same-repository PR merged as this commit |
| Manual release | Successful main CI for the exact tag commit |

Quality checks always run first. The latest eligible source run must have finished
successfully; an older successful run cannot conceal a newer failure, cancellation
or running validation. Only `ci.yml` runs from this repository are eligible.
PR promotion additionally checks the merged PR, its source repository and head,
and the tested merge commit's parents. Fork PRs receive fresh main validation.

The consumer requires an identical full Git tree, matching Core/launcher versions
and the same sibling-dependency owner. The tree includes source, dependency pins,
locks, tests, build scripts and workflow definitions. Squash merging can change
the commit ID while retaining this identity. A different tree, including a
version bump, requires fresh validation; merely sharing a version is insufficient.

The resolver downloads by artifact ID and checks GitHub's archive SHA-256 before
extracting regular files into a temporary directory. Unsafe paths, links,
duplicate archive members and oversized bundles are rejected. It then verifies
the producer against GitHub run/commit metadata and checks the complete file
inventory, release checksums, expected platforms and installed-executable evidence.
See GitHub's [artifact API](https://docs.github.com/en/rest/actions/artifacts)
and [workflow-run API](https://docs.github.com/en/rest/actions/workflow-runs).

## Evidence and retention

`candidate.json` records the source tree, version, dependency owner, producer
commit/run/attempt, every file's size and SHA-256, and any preceding promotion's
run, artifact ID and digest. Promotions copy the validated bytes and original
test reports unchanged, then record the new producer. The retained reports describe
the original execution; they do not claim tests ran again on a promotion runner.

Each stage uploads its own `validated-release` bundle with a 30-day retention.
Later stages therefore do not depend on all previous copies remaining available.
Individual fresh-build artifacts retain their existing retention periods.

No eligible retained bundle, an integrity mismatch, or an API/download failure
selects the complete Python, PostgreSQL and native validation workflow. The final
gate accepts only successful verified reuse or successful completion of every
fresh check and bundle creation. Set `rebuild` on a manual CI/publication dispatch
to force fresh validation. Re-running all jobs also bypasses reuse; rerunning only
failed publication jobs can retain the already validated bundle in that run.

## Publication consumers

Publication remains an explicit manual operation on the exact version tag.
Core, launcher, GitHub Release and container jobs download the bundle from their
own publication run. Before staging any files, they verify its identity, hashes
and producer run. Core receives exactly its wheel and sdist; the launcher receives
all four wheels; Release jobs receive the archives, manifests, checksums and Intel
companion. Container jobs select only their architecture's verified release files.

The Intel companion still uses the same versioned Core Release URL and bytes.
Native wheel/archive executable identity and installed-wheel acceptance remain
requirements of the original validation. Container images and their security
checks still run during publication; the reused inputs are the native executables.

For a failed publication, retain `validated-release` from that run and inspect its
provenance and checksums. Recover only the failed destination using those bytes.
PyPI distributions are immutable: do not rebuild and blindly re-upload an already
partially published version. See [releasing](releasing.md) for publication order.
