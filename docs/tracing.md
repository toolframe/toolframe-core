# Optional OpenTelemetry tracing

Toolframe can trace HTTP requests, plugin service calls, nested dependencies and
managed plugin tasks without changing business plugins. Tracing is disabled by
default. The Python Core and SDK workers emit spans; the Rust launcher selects
the optional dependencies and starts Core.

## Start with a collector

Start an OTLP HTTP/protobuf collector, then launch the application:

```sh
export TOOLFRAME_TRACING_ENABLED=true
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:4318/v1/traces
export OTEL_TRACES_SAMPLER=parentbased_traceidratio
export OTEL_TRACES_SAMPLER_ARG=1.0
./toolframe --home ./my-app serve
```

The standard executable resolves the tracing extra on first enabled startup.
Python-only installations need `toolframe-core[tracing]`; local source development
can run `uv sync --extra tracing`. Restart Core after changing tracing settings.
The collector address above is an example for a local collector; a container
must publish its OTLP port to the host. Toolframe does not launch a collector.

Use `OTEL_EXPORTER_OTLP_TRACES_HEADERS` for exporter authentication, for example
`authorization=Bearer%20TOKEN`. Keep it in the process environment or a secret
manager. It does not belong in `toolframe.toml` or the endpoint URL.

## Configuration

| Environment variable | Behavior |
| --- | --- |
| `TOOLFRAME_TRACING_ENABLED` | `true`/`1` enables tracing; unset, empty, `false`/`0` disables it. Other values fail startup. |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | Full trace URL, normally ending in `/v1/traces`. Required unless the generic endpoint is set. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Generic base URL; the exporter appends `/v1/traces`. The trace-specific setting takes precedence. |
| `OTEL_EXPORTER_OTLP_TRACES_HEADERS` | Standard comma-separated, URL-encoded exporter headers; generic `OTEL_EXPORTER_OTLP_HEADERS` is the fallback. |
| `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` | Only `http/protobuf` is supported. Generic `OTEL_EXPORTER_OTLP_PROTOCOL` is the fallback. |
| `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG` | Standard SDK sampling configuration. Default is `parentbased_always_on`; ratio sampling accepts `0.0` through `1.0`. |
| `OTEL_RESOURCE_ATTRIBUTES` | Additional resource metadata, such as `deployment.environment.name=development`. |
| `OTEL_SERVICE_NAME` | Optional Core service name override. Worker service names remain `toolframe.plugin.<plugin-id>`. |
| `OTEL_BSP_MAX_QUEUE_SIZE` | Maximum queued spans per process; default `512`. |
| `OTEL_BSP_MAX_EXPORT_BATCH_SIZE` | Default `128`, capped by queue size. Must be positive and no larger than the queue. |
| `OTEL_BSP_SCHEDULE_DELAY` | Batch interval in milliseconds; default `1000`. |
| `OTEL_EXPORTER_OTLP_TRACES_TIMEOUT` | Export timeout in seconds, in `(0, 5]`; default `2`. Generic `OTEL_EXPORTER_OTLP_TIMEOUT` is the fallback. |
| `OTEL_SDK_DISABLED=true` | Disables instrumentation while keeping the selected dependency profile. Prefer the Toolframe flag to avoid installing optional dependencies. |

Standard exporter compression, server CA and client certificate/key settings are
also forwarded to workers. Only the SDK's trace configuration allowlist is
forwarded. No exporter configuration or credentials are passed to package builds
or import/configuration preflight workers. Trace IDs travel with individual
requests, never through environment variables. Endpoint and authentication
changes do not require rebuilding plugin packages.

## Reading a trace

Core's default service name is `toolframe.core`. All processes include the
application ID as `service.namespace` and `toolframe.application.id`; workers
also include plugin ID and generation. Filter by application ID when several
Toolframe applications share a collector.

An HTTP call to a business service creates a server span with the route template,
then client/server pairs across Core and worker boundaries. Nested dependency
and storage service calls stay in that trace. MCP requests propagate through the
HTTP gateway into their provider and business calls; FastMCP may add its own
spans. Dashboard actions over its WebSocket produce traces when they call Core
services. WebSocket connections themselves are not long-lived spans, and
`/healthz` is excluded. Managed tasks start separate traces.

Toolframe instrumentation records operation names, identities, response status
and error categories. It excludes arguments/results, SQL statements, headers,
cookies, exception messages and stack traces. Third-party instrumentation or
plugin-created spans can record additional data; their policies are separate.
This release provides tracing only, without automatic metrics, log export or
instrumentation of arbitrary third-party network/database calls.

## Cost and failure behavior

With tracing disabled, the base Core/SDK install has no OpenTelemetry SDK or
exporter and the tracing implementation is not imported. A provider dependency
may independently use the small OpenTelemetry API package. Enabling tracing adds
dependencies, a provider, a bounded queue and an export thread to each process.
Sampling reduces recorded/exported spans; propagation and span creation still
have some cost. Measure representative workloads when choosing a sampling ratio.

The launcher and plugin environment caches distinguish enabled and disabled
profiles. Switching back selects a base environment; existing cached tracing
environments are retained on disk. The shared uv download cache avoids repeated
downloads, while each worker retains its own runtime state. Previously installed
plugin artifacts and business data survive profile changes.

Export happens asynchronously. Collector outages do not fail business calls, but
queues can drop spans. Shutdown attempts a best-effort drain for two seconds,
plus at most one active export timeout; remaining spans may be lost. There is no
durable trace spool. Invalid startup configuration fails clearly.

## Verification

Run the regular checks described in [testing](testing.md). `tests/test_tracing.py`
uses real immutable worker environments and an OTLP HTTP receiver to verify a
nested HTTP trace, error reporting, secret exclusion and off/on/off transitions.
The SDK separately tests concurrency, timeouts, malformed carriers and sampling.
For deployment verification, invoke a business operation and inspect its full
trace in your collector, including the Core and plugin service names.
