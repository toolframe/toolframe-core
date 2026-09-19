# Toolframe core

A persistent local control host for replaceable Python plugins. Core has no
Streamlit, FastMCP, business schema or demo imports. It starts empty, keeps its
management database in `data/core.sqlite`, and runs each plugin/provider in an
immutable environment and separate process under fastapi-cordis.

```sh
uv sync --group dev
uv run toolframe-core --home /path/to/app init
uv run toolframe-core --home /path/to/app serve
# Another terminal:
uv run toolframe-core --home /path/to/app plugin add /path/to/plugin
uv run toolframe-core --home /path/to/app plugin enable plugin-id
uv run toolframe-core --home /path/to/app plugin validate plugin-id
uv run toolframe-core --home /path/to/app plugin reload plugin-id
```

`toolframe.toml` is desired authority. `toolframe.lock` records exact artifacts and
resolved environments; SQLite records observed generations, candidates and the
operation journal. Installing defaults to disabled. Existing-data migrations use
`migrate plan ID` then `migrate apply ID --plan-id HASH`. Disable/remove preserve
business databases. Failed updates do not silently rewrite desired configuration.

Local path, Git (`git+URL@ref#subdirectory=path`), wheel paths/HTTPS and
`pypi:distribution==version` sources share capture/build/validation. Git references
resolve to commits. Local source changes get new content identities even at the
same package version. Wheels must export one `toolframe.plugins` entry point and
use the public SDK. Providers are sibling repositories, not bundled source.

Management API: authenticated `/_toolframe/status`, `/_toolframe/operations`,
`/_toolframe/operations/{id}`, and explicit cancellation. Operations survive client
disconnects. `toolframe open` prints a local Dashboard sign-in link. The token is
stored with owner-only permissions in `.toolframe/management.token`. Version 1
binds loopback only; the gateway validates Host/Origin and protects management and
Dashboard access. `/healthz` is core liveness, separately from application readiness.

The SDK, SQLite provider, llms provider, MCP provider, Dashboard and Entry Register
each live in their own repository. For unpublished local packages, configure
`[packages] find_links=["/absolute/wheelhouse"]` containing their built wheels.
Source checkouts are used only to build captured wheels; no live editable plugin
is installed into a worker.

The Rust executable only bootstraps pinned Python/core packages and forwards to
the Python CLI. `scripts/build-release.sh` embeds core, SDK and Cordis wheels;
provider implementations are independently distributed. First use may download
Python/dependencies. `--offline` uses existing caches, never claims missing content
is available. `--wheelhouse PATH` supports local development builds. The formal v1
release targets remain macOS 12+ arm64 and Intel; cross-platform support needs
separate acceptance evidence.
