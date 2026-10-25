# Run Toolframe in a container

Toolframe images package the native launcher, uv, Python and the Core runtime.
They run an application from `/data` and expose its HTTP gateway on port 9876.
Both variants support Linux x86-64 (`amd64`) and ARM64 (`arm64`). Docker Desktop
can run the Linux images on macOS and Windows.

## Quick start

Create a named volume, initialize an application and start the server:

```sh
docker volume create toolframe-data
docker run --rm --cap-drop ALL --security-opt no-new-privileges:true \
  -v toolframe-data:/data \
  ghcr.io/toolframe/toolframe:0.8.1 init --template entry-register
docker run -d --name toolframe --restart unless-stopped \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --stop-timeout 40 -p 127.0.0.1:9876:9876 -v toolframe-data:/data \
  ghcr.io/toolframe/toolframe:0.8.1
```

Open `http://localhost:9876`. To obtain the management sign-in link:

```sh
docker exec toolframe toolframe --home /data open
```

Omit `--template entry-register` for an empty application. The image contains
Python and Core but does not contain the template plugins or an initialized
application. First installation of plugins needs access to their package sources.
Initialization is explicit; startup never overwrites an existing configuration.

The default command is `serve --host 0.0.0.0`. The example publishes the gateway
only on the host's loopback interface. For LAN access, publish `9876:9876` and
follow the [network and access guidance](../README.md#lan-access).

## Choose an image

| Tag | Contents and intended use |
| --- | --- |
| `0.8.1` | Debian 13 slim, shell, Git, process tools and the Toolframe runtime. Supports Git plugin sources and interactive diagnosis. |
| `0.8.1-distroless` | Google Distroless Debian 13 C/C++ runtime, Toolframe, uv, managed Python, Tini and the `ps` dependencies required by Core. No shell, Git or OS package manager. |
| `latest` | Standard image for the latest stable Core release. |
| `distroless` | Distroless image for the latest stable Core release. |

Use a version tag for upgrades you control, or a digest to select exact image
bytes. Version tags may be rebuilt for updated base images and dependencies;
they are not immutable. Both variants use the same native release executable
and Python/Core preparation path. Base images and uv are pinned by
multi-platform digest in the Dockerfile.

To use Distroless, replace the image in both quick-start commands with
`ghcr.io/toolframe/toolframe:0.8.1-distroless`. It retains uv because Toolframe
uses it for plugin environments. Python wheels and compatible pure-Python builds
work; Git sources, plugins invoking a shell, and native source builds needing
compilers or missing system libraries require another image. Neither variant
includes a compiler toolchain. Distroless reduces OS tooling; it does not sandbox
trusted plugin code.

Both variants install Python packages with uv. They omit pip and ensurepip,
including pip's separate vendored dependency copies. Plugins that invoke pip
directly need adaptation to uv or a custom image with a maintained pip install.

Use `docker logs toolframe` and the CLI for diagnosis:

```sh
docker exec toolframe toolframe --home /data status --json --check
```

Distroless has no `sh` or `bash` to enter. Core's `ps` dependency is included to
reconcile surviving worker ownership; removing it prevents startup.

## Data, permissions and upgrades

Both images run as UID/GID `65532:65532`. A new named volume inherits `/data`'s
ownership. Bind-mounted directories must already be writable by that user; the
entrypoint never changes ownership of existing host files.

Persist all of `/data`, including configuration, the Core journal, business data,
keys, retained packages, plugin environments and the uv cache under
`.toolframe/uv-cache`. That cache also supports re-resolution of the shared
Dashboard environment after container replacement. Keep the mount at the same
container path across replacements because Python environments contain absolute
paths. One Core owns a volume at a time. Do not mount a host application's Python
environments directly into a Linux container.

The prepared Core and managed Python live under `/opt/toolframe`, separately
from application state. The default SQLite profile is ready without network
access. Optional PostgreSQL or tracing profiles prepare their additional Core
environment on first use and may need network access. New plugins and missing
dependencies can also require network access. `--offline` does not make unprepared
plugins available; see [runtime preparation](runtime.md).

The image needs writable runtime/cache directories and `/tmp`; a read-only root
filesystem is not supported by the default configuration. Do not put an empty
volume over `/opt/toolframe`: it hides the prepared runtime. Python and Core
runtime updates are delivered with a replacement image.

Back up the application before an upgrade. Stop and remove the old container
while retaining the named volume, then create its replacement with the new image
and the same mount. Core's existing migration and recovery rules still apply;
do not run `init` over the retained application.

## Tini, health and shutdown

Both images use this executable entrypoint:

```text
/usr/bin/tini -- /usr/local/bin/toolframe --home /data
```

Tini runs as PID 1, forwards termination signals to Core and reaps orphaned child
processes. The Rust launcher replaces itself with Python using `exec`, so there
is no shell between Tini and Core. Core owns worker draining and process-group
cleanup; Tini uses direct-child forwarding rather than `-g` to leave that
ordering to Core. An additional Docker `--init` is unnecessary.

Allow at least 40 seconds for `docker stop`; applications with longer drain
budgets may need more. The shell-free health check probes `/healthz` using the
actual listener recorded in `/data/.toolframe/endpoint.json`. It reports Core
liveness, not plugin readiness. Use `status --check` or the authenticated
readiness endpoint for application readiness.

## Build and verify locally

Run these commands from this repository with Python 3.11+ and Docker Buildx.
Preparation downloads the matching published release, verifies the archive
SHA-256 and executable manifest, and stages only the binary and licenses:

```sh
python3 scripts/prepare-container.py
docker buildx build --platform linux/arm64 --target standard --load \
  -t toolframe:local .
docker buildx build --platform linux/arm64 --target distroless --load \
  -t toolframe:local-distroless .
python3 scripts/verify-container.py --image toolframe:local \
  --variant standard --platform linux/arm64 \
  --output .local/container/standard.json
python3 scripts/verify-container.py --image toolframe:local-distroless \
  --variant distroless --platform linux/arm64 \
  --output .local/container/distroless.json
```

Use `linux/amd64` on x86-64. Emulated runs check compatibility but do not replace
native release checks. `--release-dir PATH` uses already downloaded CI/release
artifacts. `--architecture amd64` or `--architecture arm64` prepares one platform.
When selecting another release, pass matching versions to the staging script
(`--version`) and Docker (`--build-arg TOOLFRAME_VERSION`).

Verification creates and removes uniquely named containers and a disposable
volume. It checks non-root execution, cold offline Core startup, Tini orphan
reaping, Dashboard HTTP, an independent MCP connection, llms, plugin reload,
graceful shutdown and data retention after recreation without a network.
Interactive Dashboard checks and optional plugins need separate acceptance checks.

## Security checks and updates

The standard build applies available Debian package upgrades before installing
runtime tools. Both images remove setuid/setgid permissions from the copied
runtime tools and run as an unprivileged user. The quick start drops all Linux
capabilities and enables `no-new-privileges`; the container verification suite
uses these same settings. Keep the host's default seccomp policy enabled. Host
kernel updates and container-engine isolation remain the operator's responsibility.

Container CI scans the exact tested image with Trivy before uploading
it. Publication fails for any Critical finding, any finding with a published
fixed version (including Unknown severity), an unsupported OS, or an incomplete
scan. Findings without a fix remain in the report; a passing check does not mean
the image has no known vulnerabilities. Review Debian's advisory and affected
code paths when assessing applicability. The policy has no blanket exclusions.

The **Container security** workflow scans both architectures of the public
`latest` and `distroless` images daily at 03:23 UTC and can also be run manually.
It has read-only repository permissions and never publishes an image. A failure
appears in GitHub Actions and the README security badge; maintainers can enable
workflow failure notifications in their GitHub notification settings. Unfiltered
JSON, scanner/database metadata and a CycloneDX inventory are retained for seven
days. These artifacts are inventories, not signed image attestations.

Dependabot checks Docker base/uv pins and GitHub Actions dependencies every
Monday at 04:23 UTC. Update pull requests require review, all image checks and
an explicit release or container-only publication run. Nothing auto-merges or
auto-publishes. The uv update range follows the released launcher's compatibility
contract; a new uv minor series needs a compatible launcher release. Also review
the pinned Trivy version and the launcher's Python version when maintaining the
image. CI refreshes the runtime build stage so Debian security upgrades can be
picked up even when a base digest has not changed. For a local security rebuild,
add `--pull --no-cache-filter runtime` to the Buildx command to avoid reusing an
older package-install layer.

Trivy covers recognizable OS packages and language dependency metadata. The
Distroless build includes package records for copied Tini, `ps` and libraries;
some records describe a full Debian package when only its runtime files are
included. Native libraries bundled inside managed Python, the compiled launcher,
plugins installed into `/data` after startup and the host kernel need separate
assessment. The daily scan covers the two current stable aliases; older retained
version tags need their own scan if still deployed.

To apply the same policy locally after building an image, use Trivy 0.76.0:

```sh
mkdir -p .local/container/security
trivy image --image-src docker --scanners vuln --ignorefile '' --list-all-pkgs \
  --format json --output .local/container/security/scan.json toolframe:local
python3 scripts/check-container-security.py .local/container/security/scan.json
trivy convert --format cyclonedx --output .local/container/security/sbom.cdx.json \
  .local/container/security/scan.json
```

## Publish to GitHub Container Registry

The production `publish.yml` calls `container.yml` after Python distributions
and GitHub Release assets succeed. It uses that run's verified Linux executables.
Native ARM64 and x86-64 jobs build, test and scan both variants; version and stable
alias tags are assembled only after all four jobs succeed. Intermediate
`build-RUN-ATTEMPT-VARIANT-ARCH` tags identify each tested image.

Pull requests changing container packaging run the same four checks against the
latest published Core release, without pushing images. Core source validation
runs separately, so a version bump does not require publishing Python packages
just to test Docker packaging.

For a container-only rebuild of an already published version, run the
**Container images** workflow on a ref whose Cargo version matches that release.
Its default is validation only; enable `publish` to upload. This also recovers
container publication failures without republishing immutable PyPI files.
A failed container job does not undo a successful Python/GitHub release.

The workflow logs in with `GITHUB_TOKEN` and `packages: write`; no personal token
is required in CI. The image source label links the package to this repo.
The registry namespace follows the repository owner, including on forks.

After the first push, open **Packages → toolframe → Package settings** in the
organization and set visibility to **Public**. Public repository visibility does
not automatically make a new container package public. Confirm an anonymous pull
before sharing the quick start. Organization policies may restrict publication.

Local pushes require credentials with package write access. Use Docker's
password-stdin authentication or use the workflow; never put credentials in the
Dockerfile or build context. The allowlisted `.dockerignore` sends only the
verified executables, licenses, health probe and runtime hardening script to Docker.

References: [GHCR](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry),
[uv containers](https://docs.astral.sh/uv/guides/integration/docker/),
[Distroless](https://github.com/GoogleContainerTools/distroless), and
[Tini](https://github.com/krallin/tini).
