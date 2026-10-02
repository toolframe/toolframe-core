# Runtime preparation and uv selection

The Toolframe executable prepares Python and package environments automatically.
It reuses an installed **stable uv version `>=0.12.2,<0.13`** when available.
This runtime range is separate from the exact build-tool versions used by CI.

## Selecting uv

The launcher selects an executable in this order:

1. `--uv-path PATH`, or `TOOLFRAME_UV` when the flag is absent.
2. The first compatible executable named `uv` on `PATH` (`uv.exe` on Windows).
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
