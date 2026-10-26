<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/readme-banner-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="docs/assets/readme-banner-light.png">
  <img src="docs/assets/readme-banner-light.png" alt="Toolframe Core — a persistent host for replaceable Python plugins. Python packages → managed environments → plugin workers." width="1200">
</picture>

# Toolframe Core

Toolframe runs Python plugins behind a persistent local host. Build applications
with Web pages, Streamlit dashboards, MCP tools and shared services, then update
plugins while retaining their stored data.

Core manages package environments, plugin processes, configuration and operation
history. Presentation, storage and authentication come from separate plugins.
Plugins execute trusted Python code; process isolation is not a security sandbox.

## Quick start

Install the native CLI with [uv](https://docs.astral.sh/uv/getting-started/installation/),
create an application and start it:

```sh
uv tool install toolframe
toolframe --home ./my-app init --template entry-register
toolframe --home ./my-app serve
```

In another terminal, get the local sign-in link:

```sh
toolframe --home ./my-app open
```

The Entry Register example includes a Streamlit Dashboard, MCP tools, llms
documents, SQLite storage and access-password management. The server listens on
`127.0.0.1:9876` by default. Omit `--template entry-register` to create an empty
host and choose your own plugins.

Pass `--home PATH` before the subcommand. Keep using the same application directory
to retain its configuration and data.

### Platforms and runtime

| Platform | Native CLI support |
| --- | --- |
| macOS 12+ | Apple Silicon and Intel |
| Linux with glibc 2.28+ | ARM64 and x86-64 |

Windows and musl Linux are not supported. First use needs network access: the
launcher prepares uv if needed, installs Python 3.14.7 and Core, and resolves the
selected plugins. `--offline` requires previously prepared assets. See
[runtime preparation](docs/runtime.md) for caches, local wheels and Intel macOS
companion wheels.

Use `uvx toolframe ...` without a persistent CLI installation, download an
executable from [GitHub Releases](https://github.com/toolframe/toolframe-core/releases),
or follow the [Docker guide](docs/container.md). Python library users install
`toolframe-core` on Python 3.14+; plugin authors use
[`toolframe-sdk`](https://github.com/toolframe/toolframe-sdk).

## Build a plugin

`init` installs the bundled `toolframe-plugin-design` skill in
`my-app/.agents/skills/`. Open an agent session in `my-app` and invoke
`$toolframe-plugin-design` with your requirements. The guide covers domain design,
SDK services, storage, presentation and validation in an installed application.

To start from a minimal service package:

```sh
toolframe --home ./my-app plugin new greeting
```

This creates `my-app/plugins/greeting` with an internal echo service and a test.
The default template requires no storage or presentation provider. Generation
creates source files; install and enable the plugin separately as shown below.
Generated projects belong to you and remain unchanged when templates or skills
are upgraded.

For a user interface, choose a Web or Streamlit template from its owning package.
See [plugin authoring](docs/authoring.md) for template discovery, compatibility,
builds and installation, or [Web applications](docs/web.md) for an HTML Notes
example with forms, JavaScript and CSS.

For an existing application, install or refresh the
[agent guide](docs/authoring.md#agent-guidance) with
`toolframe --home ./my-app skill install`. Customized copies require confirmation
before replacement; an approved overwrite saves a backup.

## Install and update plugins

With the application running, install the generated plugin:

```sh
toolframe --home ./my-app plugin add ./my-app/plugins/greeting
toolframe --home ./my-app plugin validate greeting --runtime
toolframe --home ./my-app plugin enable greeting
toolframe --home ./my-app plugin status greeting --json --check
```

`add` builds, installs and validates the package, leaving it disabled by default.
Runtime validation uses a temporary application and store; it does not certify
live business health. The basic `greeting` plugin exposes an internal service,
so enabling it does not add a Dashboard page or MCP tool.

After editing the plugin, stage the update and inspect its impact:

```sh
toolframe --home ./my-app plugin update greeting
toolframe --home ./my-app plan --action reload --id greeting --json
```

Review the completed plan, then replace `PLAN_ID` with its `result.plan_id`:

```sh
toolframe --home ./my-app plugin reload greeting --plan-id PLAN_ID
```

`update` stages a candidate without changing the running plugin. `reload` applies
the reviewed change and rejects stale plans. Use `enable` to restore a disabled
plugin. Required dependents drain together, so review the affected plugins before
applying an update. Replacing a presentation provider can reset Dashboard sessions
or require MCP clients to reconnect.

Sources can be local directories, Git revisions, wheels or
`pypi:distribution==version`. Workers run captured wheels in managed environments.
See the [operator guide](docs/operations.md),
[dependency diagnostics](docs/dependency-graph.md) and
[lifecycle and recovery](docs/architecture.md#workers-reload-and-recovery) for
impact analysis, draining and failure behavior.

## Configuration and data

Each application has one Core process and its own home directory:

| File | Purpose |
| --- | --- |
| `toolframe.toml` | Desired plugins, configuration and service bindings |
| `toolframe.lock` | Exact package artifacts and environments |
| `data/core.sqlite` | Default Core journal: candidates, generations and operations |

Core can use a dedicated [PostgreSQL database](docs/postgresql-state.md) for its
journal. Configuration, private keys and artifacts remain in the application home
with either backend.

After editing `toolframe.toml`, review and apply the change:

```sh
toolframe --home ./my-app plan --json
# Replace PLAN_ID with result.plan_id from the reviewed plan.
toolframe --home ./my-app apply --plan-id PLAN_ID
```

Business data belongs to each plugin's retained store. Disabling, replacing or
removing a plugin preserves that data. SQLite and PostgreSQL providers can run
together; changing a binding does not migrate an existing store. Plugins access
another plugin's data through its public services.

Migrations of existing data require a reviewed plan, a backup and explicit apply.
A committed or uncertain schema upgrade requires forward repair rather than an
automatic rollback to old code. See [SQL storage](docs/sql-storage.md) for provider
selection and migrations, and [offline maintenance](docs/offline-maintenance.md)
for stopped-application backup tooling.

## Network and access

The default listener accepts local connections. To serve on your LAN, restart
with:

```sh
toolframe --home ./my-app serve --host 0.0.0.0
```

Open `http://SERVER_LAN_IP:9876` from another device, using the server's numeric
address. The flag applies to that run; set `host` under `[application]` in
`toolframe.toml` to save it. See [LAN access](docs/operations.md#lan-access) for
IPv6, address validation and local management behavior.

Dashboard business access is public until you configure a password. MCP has its
own public/protected policy, and llms documents remain public. Management requires
local control, a management token or the appropriate system scopes. Configure
each entrance's access policy before sharing an application with private data.

See [Dashboard authentication](docs/authentication.md),
[MCP authentication](docs/mcp-authentication.md) and
[OIDC authentication](docs/oidc-authentication.md) for passwords, bearer keys,
shared sign-in and HTTPS proxy setup.

## Documentation

| Task | Guide |
| --- | --- |
| Manage plugins or automate operations | [Operator guide](docs/operations.md) |
| Prepare a runtime or use local wheels | [Runtime preparation](docs/runtime.md) |
| Run in a container | [Docker](docs/container.md) |
| Store binary files | [Object storage](docs/object-storage.md) |
| Export traces and logs | [OpenTelemetry](docs/tracing.md) |
| Understand ownership and lifecycle contracts | [Architecture](docs/architecture.md) and [glossary](docs/glossary.md) |
| Build, test or release Core | [Testing](docs/testing.md), [CI](docs/ci.md) and [releasing](docs/releasing.md) |
| Review changes between versions | [Changelog](CHANGELOG.md) |

## Contributing

See [Contributing](CONTRIBUTING.md) for development hooks, branch names and commit
conventions. The [CI guide](docs/ci.md#coordinated-source-dependencies) describes
the matching SDK and provider revisions needed for source validation.

## License

Core and the Rust launcher use the [Apache License 2.0](LICENSE); see
[NOTICE](NOTICE). The bundled
[plugin design guide](src/toolframe_core/bundled_skills/toolframe-plugin-design/LICENSE)
and [basic template](src/toolframe_core/basic_template/LICENSE) use the MIT
License. Each license applies to its own files. Generated plugins retain their
template's license; optional templates and dependencies carry their own licenses.
