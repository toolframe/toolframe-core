# Runtime preparation and uv selection

The Toolframe executable prepares Python and package environments automatically.
It reuses an installed **stable uv version `>=0.12.2,<0.13`** when available.
This runtime range is separate from the exact build-tool versions used by CI.

## Selecting uv

The launcher selects an executable in this order:

1. `--uv-path PATH`, or `TOOLFRAME_UV` when the flag is absent.
2. The first compatible executable named `uv` on `PATH`.
3. The verified private copy under `$TOOLFRAME_RUNTIME_ROOT/uv/`, defaulting to
   `$HOME/.cache/toolframe/uv/`.
4. A download of the target-specific release in `uv-release-manifest.txt`.

For example:

```sh
# Usually no configuration is needed: a compatible uv on PATH is reused.
./toolframe --home ./my-app init

# Select a particular installation, including an executable with another name.
./toolframe --uv-path /path/to/uv --home ./my-app serve
TOOLFRAME_UV=/path/to/uv ./toolframe --home ./my-app serve
```

An incompatible or missing explicit selection is an error; Toolframe does not
silently replace it. Automatic PATH discovery skips missing, broken, unstable
and out-of-range executables. Installed versions outside the supported range are
left untouched. Toolframe does not upgrade or overwrite the user's uv.

External executables are checked by running `--version` and validating the
supported version range. They do not need Astral's exact executable checksum or
target text, so compatible builds from other installers can be reused. Private
managed copies retain exact version, target and SHA-256 checks; downloaded
archives and extracted executables are verified before installation.

The selected absolute path is passed to Python core through `TOOLFRAME_UV`, so
bootstrap and subsequent plugin builds, resolution and installation use the same
executable. The Python development entry point also honors this variable and
otherwise uses PATH; the built launcher owns version-range validation.

## Python and offline startup

Toolframe continues to request the exact Python 3.14.7 architecture. Reusing uv
does not relax the Python requirement or change an existing package lock.

The minimum uv version includes Python 3.14.7 in its download catalog. Older uv
versions may work with an existing interpreter but are outside this supported
range. uv can introduce breaking changes in a minor release, so later minor
series require validation before being admitted. See the upstream
[Python installation documentation](https://docs.astral.sh/uv/concepts/python-versions/#installing-a-python-version)
and [versioning policy](https://docs.astral.sh/uv/reference/policies/versioning/).

`--offline` still searches the configured path and PATH before the private copy.
It prevents a missing managed uv download and requires Python and package assets
to be available locally. A compatible installed uv alone does not make a fresh
machine ready for offline startup. If no compatible executable or verified
private copy exists, the launcher reports an error with recovery instructions.

Platform support and release qualification remain as described in the
[testing guide](testing.md).

## Package sources and companion wheels

The launcher installs the matching `toolframe-core` version from PyPI into its
private environment. No Core, SDK, Cordis or plugin wheel is embedded. Plugins
resolve from the application's configured index and its explicit sources.
`--wheelhouse PATH` (or `TOOLFRAME_WHEELHOUSE`) supplies local wheels during
bootstrap and plugin preparation. Changing local wheel bytes changes the Core
bootstrap cache key, even if package versions stay the same.

Intel macOS release builds contain the filename, HTTPS URL and SHA-256 of a
cryptography companion wheel published beside the executable in GitHub Releases.
The payload stays outside the executable and native PyPI wheel. The launcher
checks cached bytes on every invocation, downloads a missing or damaged wheel
online, verifies its hash and atomically stores it under
`$TOOLFRAME_RUNTIME_ROOT/companions/<sha256>/`. A supplied wheelhouse can provide
the same verified artifact before publication or for offline setup. A mismatched
local wheel is rejected. `--offline` never downloads the companion.

The internal `TOOLFRAME_COMPANION_WHEELHOUSE` handoff makes this dependency
available to plugin resolution without recording cache paths in application
configuration. Old launchers' bundled-wheel handoff remains readable for
compatibility; new launchers clear inherited bundle paths. The authoring skill
and the basic template ship in the Python Core wheel with their original licenses.
Optional Web/Dashboard templates ship only in their owning distributions; see
[distribution authoring](authoring.md).

## Background task policy

Plugins can request a finite positive attempt budget through
`RuntimeContext.task(..., timeout=seconds)`. Core validates every `task.begin`
request and grants the authoritative root deadline only after admission. The
operator controls the upper bound in `toolframe.toml`:

```toml
[runtime]
max_task_timeout = 3600
```

The default is 3600 seconds; allowed policy values are 30 through 86400 seconds.
An omitted task budget keeps the existing 30-second root deadline. Invalid task
budgets fail with `invalid_task_timeout`; a budget above the policy fails with
`task_timeout_exceeded` before acquiring dependency leases. A waiting request is checked again against the
policy in force when admission reopens. A policy change does not shorten roots
already admitted. SDK callers that request an explicit budget
also enforce that deadline around their whole attempt.

A background poller must stop scheduling during quiescence and submit each unit
of work as a separate bounded attempt. Existing roots retain their generation
bindings while they drain; an idle poller must not hold a root. Task completion,
deadline expiry and cancellation settle the same root cleanup used by ordinary
service calls. Read-only activation/health probes and stale-generation checks
retain their existing admission rules.
