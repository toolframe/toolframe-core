# Platform glossary

Toolframe hosts provider plugins and business plugins as one application. This
glossary defines their shared terminology. Entry Register is an ordinary template
business example; its behavior belongs to its own package documentation.

## Language

**Control Host**:
The always-available part of a Toolframe application that manages plugins and
reports their state even when optional providers are absent or unavailable.

**Plugin Worker**:
A replaceable execution instance that runs a business plugin or provider while
the control host and unrelated plugins remain available.

**Dashboard Runtime**:
The shared execution space in which the Dashboard provider renders business
plugins' ordinary Streamlit pages.

**Plugin Package**:
The distributable code for one Toolframe runtime plugin in the current contract.
_Avoid_: Installed plugin when referring to a running instance.

**Plugin SDK**:
The shared public authoring contract that lets independently distributed plugins
declare capabilities and collaborate with a compatible Toolframe application.

**Plugin Candidate**:
A specific captured implementation and its resolved dependencies/configuration
being validated for activation, distinct from the implementation currently serving.

**Application Composition**:
The declared set of plugins, their enabled intent, configuration and provider
bindings for one Toolframe application.

**Runtime Plugin**:
A unit of application behavior with its own lifecycle and declared dependencies.
_Avoid_: Package when referring to lifecycle state.

**Provider Plugin**:
A runtime plugin that supplies a shared capability consumed by other plugins.

**Business Plugin**:
A runtime plugin that owns a business capability, its data meaning, and the
business operations it exposes to consumers.

**Business Service**:
The public business operations through which another plugin accesses a business
plugin's capability and data.
_Avoid_: Shared database access as a synonym for a business service.

**Storage Provider**:
A provider plugin that supplies isolated persistence for data owned by business
plugins.

**Business Store**:
The durable data owned by one stable business-plugin identity in one application,
retained independently of that plugin's installation and runtime lifecycle.

**Core State**:
Durable information used to manage plugins, separate from the business data
those plugins own.

**Plugin Code Version**:
The release identity of a plugin package's code.
_Avoid_: Version without qualification; the Entry Register already uses Version
for the concurrency value of an Entry.

**Data Schema Version**:
The identity of the data format expected by a business capability.
_Avoid_: Plugin code version as a synonym for data compatibility.

**Hot Reload**:
Applying a changed plugin while its control host and unrelated capabilities
remain available, with an observable result for the affected capabilities.
_Avoid_: Enable or retry when no changed code is loaded.

**Active Generation**:
A particular live execution of a captured plugin implementation, distinct from
its package version and any later execution of the same code.

**Affected Set**:
The capabilities and sessions whose availability can change during a management
operation because of their dependency on its target.

**Recovery**:
Restoring an available capability after a failed change using a known compatible
implementation.
_Avoid_: Data rollback as a synonym; recovery does not imply undoing committed data.

**Validation Level**:
The kind and depth of checks actually performed on a candidate, with a result
that applies only to the captured inputs those checks examined.

**Required Capability**:
A service or presentation entrance the application declares necessary before
it considers itself ready for use.

**Application Readiness**:
Whether the application's required capabilities are currently available,
distinct from the control host being alive and able to report problems.

## Application access

**Application Auth Provider**:
A provider plugin that supplies authentication and authorization for a Toolframe
application.
_Avoid_: Dashboard login as a synonym for the application's shared auth capability.

**Dashboard Auth Plugin**:
A plugin that applies application access rules to the Dashboard and provides the
operator-facing controls for managing that access.
_Avoid_: Application Auth Provider when referring only to Dashboard integration.

**Access Password**:
A named, individually revocable credential granting access without a user account;
it has access scopes and may have an expiry. Its use identifies the credential,
not the person using it. Visitors provide only its secret, which identifies at
most one currently valid credential; its name is an operator-facing label.
_Avoid_: User password, account, or user identity.

**Password Reuse**:
Creating a new Access Password with the secret of an expired or revoked
credential. It creates a new authorization; existing sessions are not transferred
to that authorization.

**Last Login**:
The most recent successful password authentication for a credential.

**Last Use**:
The most recent successful login or authorized functional activity for a
credential, excluding idle connections, polling and rejected attempts.

**Access Scope**:
A named permission that determines which application capabilities an Access
Password permits.

**All Business Scopes**:
A dynamic grant covering current and future business scopes, excluding system
scopes. Individually selected scopes remain explicit grants.
_Avoid_: All permissions, which would also include system privileges.

**System Scope**:
A permission to administer plugins, migrations, application settings or access
passwords, excluded from default business grants and explicitly delegable by an
operator.

**Local Control**:
Operator access granted by the enabled host-machine access exception, identified
in the Dashboard as local control rather than as a password login.
_Avoid_: User account or verified operating-system identity.

**Public Access Mode**:
The Dashboard access policy that admits visitors without an Access Password.

**Configured Access Policy**:
The operator's chosen public or protected Dashboard policy, retained independently
of a temporary auth outage.

**Effective Access Mode**:
The access behavior currently applied to Dashboard visitors, including public
fallback or waiting for an auth transition.

**Protected Access Mode**:
The Dashboard access policy that requires an accepted credential except for
explicit access exceptions. Expiring or revoking all Access Passwords does not by
itself select Public Access Mode.

**Public Fallback**:
Public Dashboard access following an auth-plugin error or disablement, accompanied
by a persistent public-access notice in the page header. It preserves the
Configured Access Policy, which takes effect again when auth recovers.
_Avoid_: Successful authentication or a grant of system privileges.

## Federated User and Agent Grant

A **Federated User** is a local identity linked to a verified issuer/subject pair,
with explicit local permissions. An **Agent Grant** records that user's consent
for one client, issuer, resource, scope set and expiry. A **Delegated Principal**
is the short-lived admission identity derived from a local access token and its
still-valid grant. Upstream identity tokens prove login; they do not grant app
resource access.

- **Core journal backend**: Core's own durable management-state implementation,
  selected before plugin activation. SQLite and PostgreSQL implement the same
  asynchronous interface; it is independent of a plugin storage provider.
- **Core ownership marker**: The durable run/node identity paired with a
  PostgreSQL session lock. Unclean release requires explicit recovery after
  verifying the former Core and its workers have stopped.

**Web application**: An owned `web.app` resource whose SDK factory defines full
documents, bounded HTTP handlers and app-specific assets. It runs in the Web
presentation cohort and calls domain services through Core-issued request context.

**Web revision**: Core's identity for an app generation, prepared route/asset
catalog and gateway selection. Unsafe submissions must prove the displayed
revision; a changed revision requires a document reload.

**Asset set**: A sealed manifest and allowlisted browser files whose combined
digest names immutable URLs. Relative file relationships and license references
are preserved during author-time preparation.
