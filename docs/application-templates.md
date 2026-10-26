# Application recipes and version selection

Create an application from a named recipe with:

```sh
toolframe --home ./my-app init --template entry-register
toolframe --home ./my-app serve
```

Named recipes are composition assets in plugin distributions. The recipe owner
declares each plugin's supported range, with a verified minimum and a compatibility
ceiling. Core's recipe catalog selects a compatible recipe distribution; it does
not carry per-plugin patch overrides. Entry Register's ranges and their reasons
are documented in its
[recipe contract](https://github.com/toolframe/toolframe-demo-entry-register/blob/main/docs/recipe.md).
The basic plugin scaffold and package-provided authoring templates are separate;
see [plugin authoring](authoring.md).

## Initialization

For each index range, initialization chooses the newest available stable,
non-yanked release satisfying the declared range, Python/platform metadata and
dependencies. Core's exact SDK selection is held fixed. A plugin's declared Core
dependency must also accept the running Core. Unsatisfiable newer releases allow
the resolver to select an older compatible release within the same range.

Each backend is resolved independently, including its `backend` extra when
provided and any extras explicitly requested in its source. Only page contributors
and their Dashboard provider share a `ui` solve; Web contributors share their
existing `web` cohort. Cohort constraints can lower a floating plugin selection,
which is rechecked in its own backend environment. Other workers' dependency
versions do not constrain that cohort. Imports, plugin identity, worker protocol
and configuration preflight still run; metadata compatibility does not certify
arbitrary future runtime behavior.

Initialization writes exact `pypi:distribution==version` sources, captured wheels,
their SHA-256 hashes, environment locks and journal candidates before reporting
success. It uses the existing format-1 `toolframe.lock`. The original range is
kept as `source.requirement` in candidate provenance, alongside the selected exact
`source.request`. The first `serve` reuses those selections and prepared
environments. Initialization does not activate plugins or create business stores.

If no selection is available, `no_compatible_version` identifies the requirements,
SDK, Core, Python, platform and resolver diagnostics. The composition is not
published when resolution or preflight fails. Correct the range, supply compatible
artifacts or choose a compatible Core runtime, then retry.

## Existing applications and explicit updates

`serve`, restart, `apply`, `enable`, `validate` and `reload` retain an installed
index selection when its source still matches the composition, including older
applications that contain a range. These operations verify the captured wheel;
a missing or modified artifact is an error. A missing environment is restored
from its saved lock when the Core runtime profile is unchanged. A changed Core/SDK
profile may require new dependency resolution for the same plugin artifact.

To select a new release within a named recipe's original range:

```sh
toolframe --home ./my-app plugin update entry-register
toolframe --home ./my-app plan --action reload --id entry-register
toolframe --home ./my-app plugin reload entry-register --plan-id PLAN_ID
```

Update stages a new candidate and exact source; reload activates it. The other
plugins keep their selected artifacts. The retained range is used only while its
exact source still matches the composition. To replace that policy with an
explicit pin or a different source, supply `plugin update ID --source SOURCE`.
An explicit `==version` remains exact, including an explicitly selected prerelease
or yanked release; Core does not silently reinterpret it as a range.

Local TOML recipes are copied with their sources intact. Local directory, Git,
wheel and direct URL sources retain their existing capture/update semantics.
Reloading a local development source can still capture edits. Changing source
intent is an explicit operation; an index publication alone never updates an
already selected plugin. Stores remain owned by the same plugin IDs and follow
the existing [migration and recovery contract](architecture.md#data-ownership).

## Offline use

Set `TOOLFRAME_OFFLINE=1` and supply local wheels with `TOOLFRAME_WHEELHOUSE` or
`packages.find_links`. Offline resolution uses locally available wheels and uv's
cached dependency metadata/artifacts; it cannot discover new remote releases or
withdrawals. Cached source artifacts are checked against their recorded hashes,
and every required dependency must be available locally. Missing resources produce
an error without a network fallback. Core does not discover or substitute sibling
source checkouts. Keep the application home and its artifact/lock state together.
