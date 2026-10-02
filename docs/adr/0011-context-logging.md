# ADR 0011: Context loggers with optional OTLP export

Status: accepted

## Context

Isolated workers need consistent plugin identity and request context in their
logs. Raw stderr text cannot reliably identify a Python function or recover the
active span, and parsing it would duplicate structured records already emitted
by Python logging.

## Decision

The SDK exposes a standard logger adapter as `RuntimeContext.log` and `Plugin.log`.
Pages use the same interface with additional owner labels. The runtime binds
plugin identity, async call context and Python call-site fields; authors can bind
additional immutable labels. Core instances own separate logger handlers.

Core and workers emit local JSON records. Worker stdout redirection remains a
protocol safeguard, and raw stream output remains local. An explicit OTLP logs
exporter uses the existing optional telemetry dependencies and process resource.
Active trace context is attached when the record is emitted. Logging levels are
independent of trace sampling. Queue ownership, shutdown and deployment settings
are specified in the [telemetry guide](../tracing.md).

## Consequences

Plugins need no exporter configuration or manual identity fields. Standard logger
methods and Python call-site information stay available without telemetry.
Automatic service call records use DEBUG to avoid routine INFO-level volume;
applications choose their own business events. Opted-in export adds a queue and
thread per process and can lose records during outages or abrupt termination.
Application-authored log contents remain the author's responsibility. Third-party
logger levels and existing handlers retain their own behavior.
