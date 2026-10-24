# Full-document Web presentation

## Context

Business plugins need HTML documents and browser libraries without depending on
Streamlit or running another supervised application process. Authors may have
Python and Toolframe without a Node toolchain. Browser resources must remain
portable with an installed wheel and follow the same visitor permissions as
other presentation surfaces.

## Decision

The SDK defines a small framework-independent Web contract. Core attaches owned
resources to a separate Web provider cohort and issues request-bound references
for handler admission and business calls. The provider adapts HTTP, templates,
forms and sealed asset sets. Domain services retain mutation semantics and data.

New Web presentation sources start with FSD app composition and page slices.
Python packages and native ESM expose slice public APIs without an additional
build toolchain. Reuse justifies lower layers; the starter does not create empty
layers. This keeps page changes together while retaining the existing business
service as the authority for data, transactions and permissions.

Each app owns its full document, CSS and JavaScript versions. Recommend htmx and
standalone Tailwind for simple forms while keeping them optional. Author-time
preparation accepts explicit browser-ready asset graphs, verifies pinned bytes
and stages outputs before wheel building. Serving never runs a frontend compiler
or resolves a CDN. This avoids a general package-manager/build-hook interface.

Schema 2 assigns presentation mounts explicitly. It supports Web or Dashboard
at root while keeping Core, OAuth, MCP and documentation namespaces fixed. A
failed or disabled selected app retains its route and returns unavailable.
Schema 1 remains compatible with Dashboard-only applications.

## Consequences

Web contributors share Python dependency resolution and restart together; this
keeps worker lifecycle understandable but does not isolate incompatible Web
framework dependencies. The independent Dashboard cohort remains usable during a
Web outage. Apps share a trusted origin, so the boundary is authorization and
lifecycle control rather than a sandbox for untrusted packages.

Finite HTTP responses, fixed limits and declared assets cover forms and local
browser modules. Arbitrary ASGI mounts, streaming, SSE and general WebSockets need
a separate contract. Read/write scopes, CSRF and revision checks protect calls;
service idempotency is still necessary because a disconnect cannot undo a domain
mutation already admitted.
