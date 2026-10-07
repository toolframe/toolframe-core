# ADR 0012: Optional tracing at runtime boundaries

Status: accepted

## Context

Business operations cross isolated Python processes and can call nested plugin
dependencies. Instrumenting only the HTTP entrance cannot show that work, while
requiring every plugin to install and configure telemetry duplicates lifecycle
and propagation behavior. Always installing an exporter increases the baseline
cost of every worker.

## Decision

Core and the SDK instrument their shared HTTP and JSON-RPC boundaries. W3C trace
context is separate from business arguments and authorization context. Python
owns providers and exporters; the Rust launcher selects an optional dependency
profile. Tracing remains disabled by default and uses HTTP/protobuf initially.

Each process owns a bounded, asynchronous export queue. Shutdown is best effort,
and collector failures do not fail business calls. Package builds and preflight
workers do not receive telemetry configuration or credentials. Runtime usage,
data limits and configuration are documented in the [tracing guide](../tracing.md).

## Consequences

Existing plugins receive transport tracing without code changes. Enabled workers
pay a per-process memory/thread cost, and queued spans can be lost. Tracing does
not automatically cover arbitrary library calls, metrics or logs. Immutable
environment profiles let applications switch tracing off without replacing
business artifacts or deleting data; retained caches still consume disk space.
