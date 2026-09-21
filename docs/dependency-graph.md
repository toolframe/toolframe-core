# Plugin dependency diagnostics

Core uses `fastapi-cordis.PluginManager` for lifecycle state, leases and disposal.
Its `Graph` resolves versioned service bindings and determines the replacement
cohort, including shared presentation processes. The Dashboard dependency graph
combines these two sources; it does not infer dependencies from Python imports.

## Dashboard

On **Plugin management**, expand **Plugin dependencies & impact** directly below
the status table. It starts collapsed, above the disabled-plugin explanation.

- Nodes show lifecycle state and health separately. An active process with stale
  or failed health is not colored healthy. Disabled, stopped, removed, unknown,
  and missing supplier nodes remain visible.
- Arrows point from consumers to suppliers. Required services use solid lines,
  optional services dashed lines, and presentation providers dotted lines.
- Select a plugin and choose **reload** or **disable**. Blue marks the selected
  node, orange the replacement cohort, amber optional consumers, and purple
  presentation providers whose entries may refresh without a process restart.
- The details distinguish required dependents, shared provider replacement,
  optional consumers, and affected page/tool/document entries. All pages in a
  replaced shared Dashboard may reconnect even when their business workers stay
  running. Unchanged MCP descriptors do not require an MCP process restart;
  removing tools does. Document publication can refresh without restarting llms.
- The relationship table includes exact services, contract versions and binding
  status. Use the chart's full-screen control to enlarge the overview.

Refresh status to obtain a new snapshot. The graph does not install, validate,
build, plan or apply operations. It requires the same `system.plugins.read`
permission as the status table and is not exposed through public business APIs.

## Snapshot contract

`GET /_toolframe/v1/status` and `toolframe status --json` include an additive
`dependency_graph` field with `schema_version: 1`:

| Field | Meaning |
| --- | --- |
| `basis` | `selected`: the definitions currently selected by core. |
| `nodes` | Plugin identity, selected version, desired enablement, Cordis state, health freshness, in-flight count and blockers. Unresolved service/provider nodes have `kind: unresolved`. |
| `edges` | Consumer `source`, supplier `target`, `kind` (`required`, `optional`, `presentation`), exact `label`, and binding `state` (`resolved`, `missing`, `inactive`). Presentation edges also include resource IDs and publication state. |
| `impacts[id][reload\|disable]` | `affected` from the same `Graph.affected` used by lifecycle operations, `required_dependents`, additional `presentation_providers`, `optional_consumers` of any affected worker, and `presentations` that may reconnect/refresh. |

Selected definitions always win over a newer validated candidate. For inactive
plugins, `definition_source: last_candidate` identifies historical declarations;
edges from these nodes are not runtime bindings. Unresolved declarations name a
known supplier only if it is unambiguous and matches the service version and
explicit binding. Otherwise a missing/ambiguous node is shown. Configuration,
service implementations, migration SQL, documents and credentials are excluded.

Impact is a structural view of the selected graph. It assumes the selected
definition remains unchanged for reload. A newly prepared candidate, pending
composition changes, migrations, failed workers or changed bindings can produce a
different plan. **Preview operation** remains the reviewable authority before
**Apply**. Reading diagnostics never creates an operation journal entry.

Core 0.4.0 supplies these diagnostics; Dashboard 0.3.0 renders them. The Entry
Register 0.2.2 template pins this Dashboard version. Older cores remain usable
with the new Dashboard and show an upgrade hint in the accordion.

## Verify a release

```sh
uv run python scripts/verify-dependencies.py \
  --binary dist/toolframe-0.4.0-standard-aarch64-apple-darwin \
  --output build/dependency-arm64.json
```

The probe uses a disposable application: seven bundled plugins, read-only
snapshots, exact comparison with reviewed reload/disable plans, then a real
disable/enable cycle. `--runtime-root PATH` supports an independent launcher
cache for Intel/Rosetta verification.
