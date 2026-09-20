# Keep a protected Dashboard protected after its passwords become invalid

A fresh Dashboard is public until its first Access Password is created. When auth
is running, expiring or revoking every password does not reopen protected access;
an operator must explicitly select Public Access Mode. Deriving public access
from the absence of valid passwords would turn
a restriction or expiry into an unexpected grant of access.

Auth-plugin error/disablement is a separately accepted exception that does open
access, described in [ADR 0003](0003-public-fallback-on-auth-error-or-disablement.md).
It does not turn invalid passwords or permission denials into auth-plugin failures.

Direct loopback access is a configurable, enabled-by-default operator exception;
the Dashboard labels it "本機控制". Requests through the host's LAN address are
treated as remote. The management token remains an independent recovery path.
This release verifies direct localhost/LAN HTTP only; proxy/TLS deployment is
outside this release. See [authentication](../authentication.md) for configuration.
