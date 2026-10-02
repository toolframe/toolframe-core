# Optional OpenTelemetry tracing and logs

Toolframe can trace HTTP requests, plugin service calls, nested dependencies and
managed plugin tasks without changing business plugins. Tracing is disabled by
default. The Python Core and SDK workers emit spans; the Rust launcher selects
the optional dependencies and starts Core. Structured local logging is always
available; log export is a separate opt-in within the enabled telemetry runtime.

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
also forwarded to workers. Only the SDK's telemetry configuration allowlist is
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
Metrics and arbitrary third-party network/database instrumentation are not
automatically enabled.

## Export structured logs

Plugins use `context.log` (or `self.log` on the base `Plugin`) for standard Python
logging with plugin identity and function/call labels. Core and worker logs are
locally formatted as JSON. To export them as well, retain the tracing setup above
and add:

```sh
export OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=http://127.0.0.1:4318/v1/logs
export TOOLFRAME_LOGS_LEVEL=INFO
```

Both the Toolframe master flag and the explicit logs exporter are required.
Toolframe defaults `OTEL_LOGS_EXPORTER` to `none`. `OTEL_SDK_DISABLED=true`
disables both signals. Changing logs settings requires a Core restart, but no
new dependency profile: the existing tracing extra contains the log exporter.

| Environment variable | Behavior |
| --- | --- |
| `OTEL_LOGS_EXPORTER` | `otlp` enables logs; unset, empty or `none` disables them. Other values fail startup when telemetry is enabled. |
| `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` | Full logs URL, normally `/v1/logs`. Required unless the generic endpoint is set; a trace-specific URL is not used as a logs destination. |
| `OTEL_EXPORTER_OTLP_LOGS_HEADERS` | Log exporter authentication; generic `OTEL_EXPORTER_OTLP_HEADERS` is the fallback. |
| `OTEL_EXPORTER_OTLP_LOGS_PROTOCOL` | Only `http/protobuf`; generic `OTEL_EXPORTER_OTLP_PROTOCOL` is the fallback. |
| `OTEL_EXPORTER_OTLP_LOGS_TIMEOUT` | Seconds in `(0, 5]`; default `2`, with generic `OTEL_EXPORTER_OTLP_TIMEOUT` as the fallback. |
| `TOOLFRAME_LOGS_LEVEL` | Minimum export level: `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`; default `INFO`. Local JSON output remains at `INFO`. |
| `OTEL_BLRP_MAX_QUEUE_SIZE` | Maximum queued records per process; default `512`. |
| `OTEL_BLRP_MAX_EXPORT_BATCH_SIZE` | Default `128`, capped by queue size. Must be positive and no larger than the queue. |
| `OTEL_BLRP_SCHEDULE_DELAY` | Batch interval in milliseconds; default `1000`. |
| `OTEL_LOGRECORD_ATTRIBUTE_COUNT_LIMIT`, `OTEL_LOGRECORD_ATTRIBUTE_VALUE_LENGTH_LIMIT` | Standard SDK limits on log attributes. |

The generic endpoint is a base URL; `/v1/logs` is appended for this signal.
Log-specific compression, server CA and client certificate/key variables
(`OTEL_EXPORTER_OTLP_LOGS_COMPRESSION`, `OTEL_EXPORTER_OTLP_LOGS_CERTIFICATE`,
`OTEL_EXPORTER_OTLP_LOGS_CLIENT_CERTIFICATE`, `OTEL_EXPORTER_OTLP_LOGS_CLIENT_KEY`)
take precedence over their shared OTLP equivalents. These settings and the log
level are forwarded only to active workers, never package builds or preflight.

Log resources contain the same application and worker identity as spans. The
record includes the actual Python function, module, file and line. SDK service
invocations add service/method/root labels; context is isolated between async
requests and managed tasks. An active span supplies native OTLP trace/span IDs;
records outside a span have no trace association. Log levels work independently
of trace sampling, so a record can refer to an unsampled trace.

Plugin activation and Core lifecycle transitions are logged at `INFO`. Set the
export level to `DEBUG` to include SDK service call start/completion records and
elapsed time. Failures emit an error category at `ERROR`. These automatic records
exclude arguments, results and exception messages. Application-authored messages,
extra fields and `logger.exception()` output are explicit log content.

Ordinary Python loggers that propagate to the worker's root logger share the
export handler, subject to their existing levels. Each Core instance owns its own
handlers, preserving isolation when several hosts are embedded in one process. SDK/exporter diagnostics are
excluded from OTLP to prevent recursive export. Raw `print()`/stdout/stderr text
is retained locally; it is not parsed or exported as structured application logs.

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
Enabled logs add their own bounded queue and export thread, with the same shared
shutdown deadline. Runtime close removes its logging handlers before draining
the queues. Logs also have no durable spool and may be lost on abrupt exit.

## Verification

Run the regular checks described in [testing](testing.md). `tests/test_tracing.py`
uses real immutable worker environments and an OTLP HTTP receiver to verify a
nested HTTP trace, error reporting, secret exclusion and off/on/off transitions.
It also verifies real worker log export, opt-in behavior, concurrent request
correlation, function labels and automatic error data limits.
The SDK separately tests concurrency, timeouts, malformed carriers and sampling.
For deployment verification, invoke a business operation and inspect its full
trace in your collector, including the Core and plugin service names.
