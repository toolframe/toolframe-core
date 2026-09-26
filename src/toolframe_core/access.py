"""Host-owned identity admission; plugin-owned credentials and session persistence."""

from __future__ import annotations

import hmac
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import urlsplit

from toolframe_sdk import SYSTEM_SCOPES, PluginError, Principal

from .config import atomic_write
from .network import loopback

OPERATOR_COOKIE = "toolframe_session"
PASSWORD_COOKIE = "toolframe_access"
CONTEXT_HEADER = "x-toolframe-context"
DENIALS = {
    "invalid_credentials",
    "session_invalid",
    "password_conflict",
    "credential_missing",
    "credential_revoked",
    "invalid_request",
    "forbidden",
    "rate_limited",
}


@dataclass(frozen=True)
class Identity:
    local: bool = False
    operator: bool = False
    session: str = ""


def operation_scopes(action, params=None):
    params = params or {}
    if action == "plan":
        return operation_scopes(params.get("action", "apply"))
    if action in {
        "status",
        "operation",
        "operations",
        "logs",
        "composition",
        "readiness",
    }:
        return ("system.plugins.read",)
    if action in {"migrate-plan", "migrate-apply"}:
        return ("system.migrations.manage",)
    return ("system.plugins.manage",)


class Access:
    def __init__(self, host):
        self.host = host
        self.contexts: dict[str, Identity] = {}
        self.failures = {}
        self.login_attempts = OrderedDict()
        self.login_slots = 0

    def catalog(self):
        catalog = {
            key: {"title": title, "category": "system", "owner": "system"}
            for key, title in SYSTEM_SCOPES.items()
        }
        for owner, definition in self.host.graph.definitions.items():
            for name, scope in definition.authorization.scopes.items():
                catalog[name] = {**scope.model_dump(), "owner": owner}
        return catalog

    def identity(self, connection):
        authorization = connection.headers.get("authorization", "")
        token = (
            authorization.removeprefix("Bearer ")
            if authorization.startswith("Bearer ")
            else connection.cookies.get(OPERATOR_COOKIE, "")
        )
        try:
            local = bool(connection.client and loopback(connection.client.host))
        except ValueError:
            local = False
        # A public virtual host is remote even if its proxy omits forwarded headers.
        public_url = self.host.composition.application.public_url
        if (
            public_url
            and connection.headers.get("host", "").lower()
            == urlsplit(public_url).netloc.lower()
        ):
            try:
                local = local and loopback(urlsplit(public_url).hostname)
            except ValueError:
                local = False
        # A reverse proxy's loopback socket is not a local human operator.
        if any(
            name in connection.headers
            for name in (
                "forwarded",
                "x-forwarded-for",
                "x-forwarded-host",
                "x-forwarded-proto",
            )
        ):
            local = False
        return Identity(
            local=local and self.host.composition.access.local_control,
            operator=hmac.compare_digest(
                token.encode("utf-8"), self.host.token.encode("utf-8")
            ),
            session=connection.cookies.get(PASSWORD_COOKIE, ""),
        )

    def register(self, identity):
        if len(self.contexts) >= 4096:
            raise PluginError("rate_limited", "Too many Dashboard connections")
        reference = secrets.token_urlsafe(32)
        self.contexts[reference] = identity
        return reference

    def context(self, reference):
        identity = self.contexts.get(reference)
        if identity is None:
            raise PluginError(
                "authentication_required", "Dashboard connection expired; reconnect"
            )
        return identity

    def chain(self):
        policy = self.host.composition.access
        return (
            (policy.auth_provider, "auth"),
            (policy.dashboard_auth, "dashboard-auth"),
        )

    async def mode(self):
        host, policy = self.host, self.host.composition.access
        if policy.mode == "public":
            return {
                "mode": "public",
                "reason": "configured_public",
                "configured": "public",
            }
        problems, transitioning = [], False
        for name, kind in self.chain():
            desired = host.composition.plugins.get(name)
            runtime = host.runtimes.get(name)
            if (
                not desired
                or not desired.enabled
                or (await host.state.plugin(name))["stopped"]
            ):
                problems.append(f"{name}: disabled or not installed")
                continue
            if runtime and self.failures.get(name) == runtime.generation:
                problems.append(f"{name}: auth decision failed")
                continue
            if (
                host.healthy_runtime(name)
                and runtime.candidate["definition"].get("provider_kind") == kind
            ):
                continue
            if host.starting or name in host.transitioning:
                transitioning = True
            else:
                problems.append(f"{name}: unavailable")
        if problems:
            return {
                "mode": "waiting" if policy.failure_mode == "closed" else "public",
                "reason": "auth_unavailable",
                "configured": "protected",
                "detail": "; ".join(problems),
            }
        return {
            "mode": "waiting" if transitioning else "protected",
            "reason": "auth_transition" if transitioning else "configured_protected",
            "configured": "protected",
        }

    async def mark_failed(self, name):
        runtime = self.host.runtimes.get(name)
        if not runtime:
            return
        self.failures[name] = runtime.generation
        runtime.health = {"runtime": "failed", "error": "Auth decision service failed"}
        for member in self.host.graph.dependents({name}):
            if active := self.host.runtimes.get(member):
                active.admitted = False
                self.host.gated.add(member)
                (
                    await self.host.record(
                        active, "failed" if member == name else "blocked"
                    )
                )
        self.host.health_publication_pending = True

    async def auth(self, method, params, *, principal=None):
        name = self.host.composition.access.auth_provider
        if not self.host.healthy_runtime(name):
            raise PluginError(
                "auth_unavailable", "Authentication provider is unavailable"
            )
        try:
            result = await self.host.call_service(
                name, "toolframe.auth", method, params, principal=principal
            )
            if not isinstance(result, dict):
                raise TypeError("Invalid auth response")
            if method in {"resolve", "login"}:
                actor = Principal.model_validate(result["principal"])
                if (
                    actor.kind not in {"password", "user"}
                    or not actor.credential_id
                    or (actor.kind == "user" and not actor.user_id)
                ):
                    raise ValueError("Invalid credential decision")
                if method == "login" and (
                    not isinstance(result.get("token"), str)
                    or len(result["token"]) < 32
                    or not isinstance(result.get("expires_at"), (int, float))
                ):
                    raise ValueError("Invalid session decision")
            return result
        except PluginError as error:
            if error.code in DENIALS or (
                error.code in {"plugin_reloading", "dependency_unavailable"}
                and (self.host.starting or name in self.host.transitioning)
            ):
                raise
            (await self.mark_failed(name))
            raise PluginError(
                "auth_unavailable", "Authentication provider failed"
            ) from None
        except Exception:  # noqa: BLE001 - verifier faults must select the explicit public fallback
            (await self.mark_failed(name))
            raise PluginError(
                "auth_unavailable", "Authentication provider failed"
            ) from None

    async def resolve(self, identity, *, effective=True):
        if identity.local and self.host.composition.access.local_control:
            return Principal(kind="local")
        if identity.operator:
            return Principal(kind="operator")
        actor = Principal(kind="anonymous")
        if identity.session:
            try:
                actor = Principal.model_validate(
                    (await self.auth("resolve", {"token": identity.session}))[
                        "principal"
                    ]
                )
            except PluginError as error:
                if error.code not in {
                    "session_invalid",
                    "auth_unavailable",
                    "plugin_reloading",
                    "dependency_unavailable",
                }:
                    raise
        if not effective:
            return actor
        mode = await self.mode()
        if mode["mode"] == "waiting":
            raise PluginError("auth_transition", "Access is preparing; retry shortly")
        if mode["mode"] == "public":
            return actor.model_copy(update={"all_business": True})
        if actor.kind == "anonymous":
            raise PluginError(
                "authentication_required", "Sign in with an access password"
            )
        return actor

    async def admit(self, identity, scopes=(), *, activity=False, effective=True):
        actor = await self.resolve(identity, effective=effective)
        if not actor.allows(scopes, self.catalog()):
            if actor.kind == "anonymous":
                raise PluginError(
                    "authentication_required",
                    "Operator or scoped password sign-in required",
                )
            raise PluginError(
                "forbidden", "Your account does not grant the required scope"
            )
        if activity and actor.kind in {"password", "user"}:
            # Check/touch only after the requested operation's scopes were granted.
            # Public business access continues even if the optional session expires.
            try:
                await self.auth(
                    "resolve", {"token": identity.session, "activity": True}
                )
            except PluginError:
                if (await self.mode())["mode"] != "public" or any(
                    self.catalog().get(s, {}).get("category") == "system"
                    for s in scopes
                ):
                    raise
        return actor

    async def presentation(self, identity):
        try:
            actor = await self.resolve(identity)
        except PluginError as error:
            if error.code not in {"authentication_required", "auth_transition"}:
                raise
            actor = Principal(kind="anonymous")
        catalog = self.catalog()
        return {
            **(await self.mode()),
            "local_control": actor.kind == "local",
            "actor": actor.kind,
            "user_id": actor.user_id,
            "allowed_scopes": [s for s in catalog if actor.allows([s], catalog)],
            "login_url": "/_toolframe/auth/login",
            "logout_url": "/_toolframe/auth/logout",
        }

    async def login(self, password, peer):
        now = time.monotonic()
        # Bounded peer buckets + global concurrency. Rejecting load is not an outage.
        for key in list(self.login_attempts):
            if now - self.login_attempts[key][0] >= 60:
                del self.login_attempts[key]
        start, count = self.login_attempts.get(peer, (now, 0))
        if (
            count >= 10
            or self.login_slots >= 2
            or (peer not in self.login_attempts and len(self.login_attempts) >= 4096)
        ):
            raise PluginError("rate_limited", "Too many login attempts; retry later")
        self.login_attempts[peer] = (start, count + 1)
        if (await self.mode())["mode"] == "waiting":
            raise PluginError("auth_transition", "Access is preparing; retry shortly")
        self.login_slots += 1
        try:
            return await self.auth("login", {"password": password})
        finally:
            self.login_slots -= 1

    async def manage(self, identity, action, params):
        if action.startswith("mcp."):
            return await self.host.mcp_access.manage(identity, action[4:], params)
        if action == "presentation":
            return await self.presentation(identity)
        if action == "user_catalog":
            actor = await self.admit(identity, ["system.auth.users"], effective=False)
            catalog = self.catalog()
            return {
                "scopes": {
                    name: spec
                    for name, spec in catalog.items()
                    if actor.allows([name], catalog)
                }
            }
        if action in {"users", "user_update", "grants", "revoke_grant"}:
            scope = (
                "system.auth.users"
                if action in {"users", "user_update"}
                else "system.auth.self"
            )
            actor = await self.admit(
                identity,
                [scope],
                effective=False,
                activity=action not in {"users", "grants"},
            )
            return await self.auth(action, params, principal=actor)
        if action == "catalog":
            await self.admit(identity, ["system.auth.passwords"], effective=False)
            return {
                "scopes": self.catalog(),
                "policy": self.host.composition.access.model_dump(),
                "revision": self.host.config.revision,
            }
        if action == "policy":
            await self.admit(
                identity, ["system.settings.manage"], activity=True, effective=False
            )
            if (
                not isinstance(params.get("mode"), str)
                or params["mode"] not in {"public", "protected"}
                or not isinstance(params.get("local_control", True), bool)
            ):
                raise PluginError(
                    "invalid_request",
                    "Supply public/protected mode and boolean local_control",
                )
            async with self.host.mutation:
                await self.admit(identity, ["system.settings.manage"], effective=False)

                def edit(document):
                    document.setdefault("access", {})["mode"] = params["mode"]
                    document["access"]["local_control"] = params.get(
                        "local_control", True
                    )

                self.host.composition, self.host.revision = self.host.config.edit(
                    params.get("expected_revision"), edit
                )
                (await self.host.write_lock())
            return await self.mode()
        if action not in {"create", "list", "update", "revoke"}:
            raise PluginError("invalid_request", "Unknown password operation")
        actor = await self.admit(
            identity,
            ["system.auth.passwords"],
            activity=action != "list",
            effective=False,
        )
        if action != "create":
            return await self.auth(action, params, principal=actor)
        # Serialize with composition operations; no password ever enters their journal.
        async with self.host.mutation:
            actor = await self.admit(
                identity, ["system.auth.passwords"], effective=False
            )
            revision = self.host.config.revision
            if params.get("expected_revision") != revision:
                raise PluginError(
                    "stale_revision",
                    "Refresh access settings before creating a password",
                )
            result = await self.auth(action, params, principal=actor)
            if result["first_password"]:
                try:

                    def protect(document):
                        document.setdefault("access", {})["mode"] = "protected"

                    self.host.composition, self.host.revision = self.host.config.edit(
                        revision, protect
                    )
                    (await self.host.write_lock())
                except PluginError, OSError, ValueError:
                    # Record was committed, so return its ID/one-time secret with an
                    # explicit recovery instruction instead of suggesting a blind retry.
                    result["policy_error"] = (
                        "Password created, but the policy update could not be confirmed. Refresh and explicitly confirm protected mode."
                    )
            result["access"] = await self.mode()
            return result

    def private_secrets(self, definition, *, store_exists=None):
        result = {}
        for name in definition.private_secrets:
            path = self.host.home / ".toolframe/secrets" / definition.id / f"{name}.key"
            if path.is_symlink():
                raise PluginError(
                    "secret_invalid",
                    "Private key must be an owner-controlled regular file",
                )
            if not path.exists():
                if store_exists or (
                    store_exists is None
                    and (
                        self.host.home / "data/plugins" / definition.id / "data.sqlite"
                    ).exists()
                ):
                    raise PluginError(
                        "secret_missing",
                        "Restore the plugin private key from the application backup",
                    )
                atomic_write(path, secrets.token_hex(32))
                path.parent.chmod(0o700)
            if path.stat().st_mode & 0o077:
                raise PluginError(
                    "secret_permissions", "Private key must have mode 0600"
                )
            value = path.read_text().strip()
            if len(value) != 64:
                raise PluginError(
                    "secret_invalid", "Private key must contain 32 hex-encoded bytes"
                )
            try:
                bytes.fromhex(value)
            except ValueError:
                raise PluginError(
                    "secret_invalid", "Invalid private key encoding"
                ) from None
            result[name] = value
        return result
