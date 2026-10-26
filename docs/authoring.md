# Distribution-provided plugin templates

`toolframe plugin new NAME` creates `toolframe-core/basic`: a minimal package,
plugin definition, bounded echo service and test. It requires only the SDK, with
no SQL, Web, Dashboard or MCP resources. Generate into a new directory:

```sh
toolframe --home APP plugin new greeting --json
toolframe --home APP plugin new greeting --output ./greeting
```

Choose one destination. Existing files, directories and symlinks, including
empty directories and dangling links, are rejected. A failed generation does not
publish a partial project. Generated projects belong to their authors: changing
or upgrading a template package never rewrites previous output.

## Discover and select

```sh
toolframe --home APP template list
toolframe --home APP template list --catalog
toolframe --home APP template show toolframe-core/basic --json
```

The local list reads installed distribution metadata and wheels already captured
for the application or explicit authoring requests. Captured wheels are identified
as cached, not as enabled plugins. It does not contact an index. The separate
`--catalog` list is a small offline directory of official packages. Its availability
field distinguishes templates bundled with Core, published authoring packages,
and packages that require a matching local wheel. A catalog entry never installs anything or claims that an
unpublished authoring version can be downloaded.

An explicit source reads and caches one wheel, without installing its dependencies:

```sh
toolframe --home APP template list --from wheel:/path/to/toolframe_web-0.1.0-py3-none-any.whl
toolframe --home APP template show toolframe-web/notes --from wheel:/path/to/toolframe_web-0.1.0-py3-none-any.whl --json
toolframe --home APP plugin new notes --from wheel:/path/to/toolframe_web-0.1.0-py3-none-any.whl --template toolframe-web/notes
```

For a version published with authoring metadata, or a matching version supplied
through package `find_links` / the launcher's `--wheelhouse`, the equivalent index
source is:

```sh
toolframe template list --from pypi:toolframe-web==0.1.0
toolframe --home APP plugin new notes --from pypi:toolframe-web==0.1.0 --template toolframe-web/notes
```

These commands require that wheel to be available; the catalog does not promise
its publication. Source trees, Git repositories and source archives are not
accepted for metadata discovery. Build their wheels separately first. HTTPS wheel
sources are also supported, with the existing optional SHA-256 URL fragment.
An unsupported manifest version, malformed version requirement, invalid schema,
duplicate template ID, conflicting metadata or ambiguous locally available versions
produces an explicit error. Use `--from` to select an exact artifact when versions
conflict; compatibility does not silently select among ambiguous versions.

Well-formed distributions with incompatible Core or Python requirements remain
visible in `list` and `show`. They do not prevent selecting or generating another
compatible template. Both text and JSON report the requirements, current versions,
compatibility status and reasons. Discovery remains metadata-only and local queries
remain offline, including when an incompatible distribution is present.

Text and JSON use the same manifest. Each JSON template includes `distribution`,
`version`, full `id`, English `title` and `description`, `parameters` JSON Schema,
`generator`, `source`, `schema_version`, `requires_core` and `requires_python`
(from wheel `Requires-Python`, or `null` when unconstrained). `compatibility`
contains `compatible`, `core_version`, `python_version` and an English `reasons`
array, empty when compatible. The positional `NAME` is passed as the `name`
parameter. Additional parameters use `--parameters '{...}'`;
JSON Schema defaults are documentation, not automatic values.

Generating an incompatible selected template, including one selected with `--from`,
fails with `authoring_version_incompatible` (exit 3). Its structured error includes
the distribution, version, requirements and compatibility report. The check occurs
before creating a generation staging/output directory, preparing an execution
environment or running package code. The common execution gate also checks
compatibility for source preparation.

`plugin new` returns the existing identity/path/status fields plus `template`,
containing the selected metadata. It works without an initialized or running
application. Only execution creates an isolated environment for the selected
wheel and its declared dependencies/optional `authoring` extra. Optional packages
are never installed into Core's Python environment. Fetching a template does not
register, enable, mount or start its runtime plugin. Normal Core installation,
help, local listing and basic generation do not acquire optional Web/Dashboard
packages.

## Package ownership and compatibility

Official templates require Core `>=0.8,<1` and generate plugins with SDK
`>=0.4.1,<1`. Compatible Core minor updates do not invalidate those templates.
`schema_version`, Python requirements and the minimum Core version remain checked;
removing a minor-version ceiling does not bypass these gates. Authors must raise
the minimum when a generator starts using a newer public Core capability. Keep
explicit upper bounds for known incompatibilities or a documented migration
boundary, rather than the last version tested.

| Template | Owner | Generated behavior |
| --- | --- | --- |
| `toolframe-core/basic` | Core | Provider-free internal service and test |
| `toolframe-web/notes` | Web | FSD HTML Notes, managed SQL, scopes, idempotent create and revision-checked update |
| `toolframe-streamlit-dashboard/notes` | Streamlit Dashboard | Managed SQL Notes, Dashboard page, read-only MCP resource and llms document |

The previous implicit Dashboard Notes default has changed to basic. Select the
Dashboard template explicitly when that example is wanted. `--presentation web`
and `--presentation dashboard` remain aliases for their corresponding optional
Notes templates; the providing wheel must be locally available or supplied with
`--from`. The aliases cannot be combined with `--template`.

Core ships the basic template and agent guidance. Web/Dashboard boilerplate and
its MIT licenses ship only in the owning distributions. The skill does not install
copies of those templates. Refreshing a skill removes clean obsolete sample assets
from the skill installation while preserving customized skills and all generated
projects.

After generation, build and install the business plugin separately. Basic and
Dashboard sources use `uv build --wheel`. Web sources use `toolframe --home APP
web prepare SOURCE`, or the same preparation through source `plugin add`/reload.
Install/enable the required SQL and presentation providers explicitly. See
[Web applications](web.md) and the selected package's documentation.

## Publish an authoring distribution

Package a UTF-8 JSON manifest at the fixed wheel path
`<distribution>.dist-info/extra_metadata/toolframe-authoring.json`. The distribution
name and version come from `METADATA`; do not repeat them in the manifest. A
runtime `toolframe.plugins` entry point is not required. Core uses this same
interface for its own basic template.

For Hatchling, include the source file as extra metadata:

```toml
[tool.hatch.build.targets.wheel.extra-metadata]
"authoring.json" = "toolframe-authoring.json"
```

The installed wheel does not need the source project's `pyproject.toml`.

```json
{
  "schema_version": 1,
  "requires_core": ">=0.8,<1",
  "templates": [{
    "id": "example",
    "title": "Example plugin",
    "description": "Create a small service plugin.",
    "parameters": {
      "type": "object",
      "properties": {"name": {"type": "string"}},
      "required": ["name"],
      "additionalProperties": false
    },
    "generator": "example_authoring:generate"
  }]
}
```

IDs use lowercase letters, digits and single hyphens. The full ID is the canonical
Python distribution name followed by `/` and the template ID. Manifests are bounded
to 1 MiB; unknown fields and duplicate IDs are rejected. Parameter schemas use
JSON Schema 2020-12 and only local references. Keep descriptions in English.
The manifest is authoritative for the selected version, including when the
catalog summary was produced from another version.

The generator is a synchronous function:

```python
def generate(*, destination, parameters):
    # destination is a Path to a new empty staging directory.
    (destination / "README.md").write_text(parameters["name"] + "\n")
```

It runs in a separate interpreter with the exact selected distribution and its
dependencies. Output must contain regular project files, with no symlinks. Core
publishes them exclusively into the requested new destination after successful
execution. Preserve the template's license in the output. Generators are installed
Python code, with the privileges of the invoking user; environment isolation is
dependency separation, not an operating-system sandbox.

Installed distributions can be selected without retaining their original wheel:
execution snapshots their installed `RECORD` files into a wheel before resolving
the isolated environment. Editable installs or distributions without `WHEEL` and
`RECORD` require an explicit wheel. Discovery never performs this snapshot or
imports a generator.

`toolframe_sdk.authoring` supplies optional `render_plugin`, `plugin_names` and
`write_template` helpers. `render_plugin` needs `tomlkit`, which the distribution
can declare in its `authoring` extra. A third-party generator can use only the
standard library and need not depend on SDK or Core.

## Source preparation

A distribution may additionally declare one fixed-purpose source preparation
entry, used by the wheel build pipeline:

```json
"prepare": {"entrypoint": "example_authoring:prepare"}
```

A generated source project selects the owner with:

```toml
[tool.toolframe.authoring]
source = "pypi:toolframe-web==0.1.0"
```

Core reads the owner's manifest, prepares its isolated environment and calls
`prepare(*, source, destination, cache, offline)`. `destination` is a new path
outside the source tree. The owner stages source and prepared assets there; Core
then runs the ordinary wheel build. This interface has no arbitrary command
registry. `web prepare` is a compatibility front door to this pipeline. Older Web
recipes can obtain their owner requirement from the project's `web` extra.

Official catalog updates must pass `scripts/check-authoring-catalog.py --wheelhouse
PATH` with matching wheels for all listed packages. It compares IDs, titles and
descriptions with their manifests; parameters and executable entries are never
copied into the catalog.

## Agent guidance

`init` installs the bundled `toolframe-plugin-design` skill at
`APP/.agents/skills/toolframe-plugin-design`. Open an agent session in the
application directory and invoke `$toolframe-plugin-design` with your business
requirements. Existing sessions may need to refresh skill discovery.

The guide covers domain and data design, SDK services, managed SQL, optional
Web/Streamlit/MCP resources and installed-application validation. Install it in
an existing application, or obtain a separate copy:

```sh
toolframe --home APP skill install
toolframe --home APP skill install --output ./toolframe-plugin-design --json
```

`--output` names the exact skill directory. Installation needs no running server,
application configuration or management token. Global agent settings remain
unchanged. The skill has its own semantic version in the `SKILL.md` YAML
frontmatter. `.toolframe-install.json` records that version and the SHA-256 of
every shipped file as the baseline for later updates.

After a binary update, `serve` refreshes the skill before starting Core.
Unmodified copies update automatically. Modified, added or deleted files require
confirmation in an interactive terminal; the default answer keeps the local
copy. Noninteractive startup keeps customizations, prints resolution
instructions and continues serving. It does not repeatedly ask about a
customized copy when the binary still ships the same skill. `skill install`
explicitly checks for replacement.

JSON or noninteractive `skill install` returns exit 5 and
`confirmation_required: true` when a decision is needed. After confirmation,
`skill install --overwrite` replaces the copy and saves the customized files in
a sibling ZIP archive. Use `--output` to compare or merge a separate copy instead.
Destinations that are files or contain symlinks are retained; choose a new
directory in those cases. Results report `installed`, `updated`, `unchanged` or
`preserved`.

Updates affect only the installed skill. Generated or customized business
plugins under `APP/plugins/`, or an explicit output directory, remain unchanged.
The skill's top-level `evals/` directory is development-only and excluded from
the Core wheel, installed copies and receipt. Upgrading an unmodified older
copy removes previously bundled evals and their receipt entries. Customized
copies retain the confirmation and backup protections above. See
[testing](testing.md) for delivery checks and structural-validation limits.
