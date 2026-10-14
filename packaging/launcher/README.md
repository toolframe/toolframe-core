# Toolframe

Toolframe runs local applications assembled from isolated Python plugins. It
provides a persistent control host, a Dashboard, MCP tools, documentation and
managed storage. Business data stays in the application directory when plugins
are replaced.

Install the native CLI with [uv](https://docs.astral.sh/uv/getting-started/installation/):

```sh
uv tool install toolframe
toolframe --home ./my-app init --template entry-register
toolframe --home ./my-app serve
```

In another terminal, run `toolframe --home ./my-app open` for the local sign-in
link. To run without a persistent CLI installation, use
`uvx toolframe --home ./my-app init --template entry-register` and then
`uvx toolframe --home ./my-app serve`.

The wheels contain the Rust executable itself. No Python wrapper or extension is
loaded. The same executable is available in the
[GitHub Releases](https://github.com/toolframe/toolframe-core/releases) archives
for installation without pip or uv. `toolframe --version` works without preparing
a runtime. Other commands reuse a compatible uv or download a checksum-verified
copy, then prepare Python 3.14.7 and the selected packages. First use requires
network access. No Python wheels are embedded: Core, SDK and plugins come from
PyPI. Intel macOS also downloads a separately published cryptography wheel from
the matching GitHub Release and verifies the SHA-256 pinned into the executable.

Wheels target macOS 12 or newer on Apple Silicon and Intel, and Linux with glibc
2.28 or newer on ARM64 and x86-64. Windows and musl Linux are outside this release's
supported platforms. Plugins execute trusted Python code; process isolation does
not make untrusted packages safe to run.

For plugin installation, authentication, backups and source development, see the
[Toolframe guide](https://github.com/toolframe/toolframe-core#readme). Python
developers who need the host as a library can install `toolframe-core`; plugin
authors use `toolframe-sdk` and `toolframe-testkit`.
