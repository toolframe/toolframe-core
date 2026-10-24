"""Core-owned, request-bound admission for Web handlers and immutable assets."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

from toolframe_sdk import PluginError, digest
from toolframe_sdk.web import SAFE_METHODS


@dataclass
class WebAdmission:
    identity: object
    runtime: object
    resource: object
    method: str
    path: str
    public_base: str
    revision: str
    gateway_revision: str
    deadline: float
    admitted: bool = False
    scopes: tuple = ()


class WebAccess:
    def __init__(self, host):
        self.host = host
        self.contexts = {}
        self.catalogs = {}
        self.previous = {}

    def publish(self, catalogs):
        now = time.monotonic()
        self.previous = {
            key: value
            for key, value in self.previous.items()
            if key in catalogs and value[1] > now
        }
        for key, old in self.catalogs.items():
            new = catalogs.get(key)
            if new and new.asset_digest != old.asset_digest:
                self.previous[key] = (old, now + 600)
        self.catalogs = catalogs

    def release(self, reference):
        self.contexts.pop(reference, None)

    def revision(self, resource, gateway):
        runtime = self.host.runtimes.get(resource.owner)
        return digest(
            {
                "generation": runtime.generation if runtime else None,
                "gateway": gateway.revision,
                "catalog": self.catalogs[resource.key].model_dump(),
            }
        )

    def register(self, identity, runtime, resource, method, path, mount, gateway):
        now = time.monotonic()
        for key, value in tuple(self.contexts.items()):
            if value.deadline < now:
                self.release(key)
        if len(self.contexts) >= self.host.composition.web.max_requests:
            raise PluginError("rate_limited", "Too many Web requests")
        if resource.key not in self.catalogs:
            raise PluginError(
                "presentation_unavailable", "Web resource is not published"
            )
        reference = secrets.token_urlsafe(32)
        self.contexts[reference] = WebAdmission(
            identity,
            runtime,
            resource,
            method,
            path,
            mount.prefix,
            self.revision(resource, gateway),
            gateway.revision,
            now + self.host.composition.web.request_timeout,
        )
        return reference

    def context(self, runtime, key, epoch, reference):
        entry = self.contexts.get(reference)
        if entry is None or entry.deadline < time.monotonic():
            raise PluginError("web_context_closed", "Web request ended or expired")
        if (
            entry.runtime is not runtime
            or entry.resource.key != key
            or self.host.runtimes.get(runtime.name) is not runtime
        ):
            raise PluginError("forbidden", "Web context belongs to a different request")
        if (
            epoch != runtime.epoch
            or epoch != self.host.epoch
            or self.host.gateway.revision != entry.gateway_revision
        ):
            raise PluginError("stale_catalog", "Web catalog changed; reload the page")
        owner = self.host.runtimes.get(entry.resource.owner)
        if (
            not owner
            or owner.generation != entry.resource.generation
            or not owner.admitted
            or entry.resource.owner in self.host.gated
        ):
            raise PluginError("presentation_unavailable", "Web resource is unavailable")
        return entry

    def csrf(self, entry):
        identity = entry.identity
        value = digest(
            {
                "local": identity.local,
                "operator": identity.operator,
                "session": identity.session,
                "resource": entry.resource.key,
            }
        )
        return hmac.new(
            self.host.token.encode(), value.encode(), hashlib.sha256
        ).hexdigest()

    async def principal(self, entry, scopes=(), *, activity=False):
        authenticated = entry.resource.resource.data.get("access") == "authenticated"
        principal = await self.host.access.admit(
            entry.identity, scopes, activity=activity, effective=not authenticated
        )
        if authenticated and principal.kind == "anonymous":
            raise PluginError("authentication_required", "Sign in to open this Web app")
        return principal

    async def admit(self, runtime, params):
        entry = self.context(
            runtime, params["key"], params["epoch"], params.get("access_context")
        )
        catalog = self.catalogs[entry.resource.key]
        asset = params.get("asset")
        if asset is not None:
            if entry.method not in {"GET", "HEAD"} or not entry.path.startswith(
                "/_tf/assets/"
            ):
                raise PluginError("forbidden", "Invalid asset admission")
            if entry.path != "/_tf/assets/" + asset:
                raise PluginError("forbidden", "Asset does not match this request")
            checksum, _, path = asset.partition("/")
            selected = catalog if catalog.asset_digest == checksum else None
            previous = self.previous.get(entry.resource.key)
            if (
                selected is None
                and previous
                and previous[1] > time.monotonic()
                and previous[0].asset_digest == checksum
            ):
                selected = previous[0]
            if selected is None or path not in selected.asset_paths:
                raise PluginError("web_asset_missing", "Unknown Web asset")
            # Old assets obey the currently selected access policy, never their old policy.
            if catalog.asset_visibility != "public":
                await self.principal(entry, entry.resource.resource.required_scopes)
            return {"visibility": catalog.asset_visibility}
        route = next(
            (route for route in catalog.routes if route.id == params.get("route")), None
        )
        fallback = (
            params.get("route") == "@spa"
            and catalog.spa_fallback
            and entry.method in {"GET", "HEAD"}
            and not entry.path.startswith("/_tf/")
            and all(item.match(entry.path) is None for item in catalog.routes)
        )
        if not fallback and (
            route is None
            or route.match(entry.path) is None
            or (entry.method != "OPTIONS" and entry.method not in route.allowed_methods)
        ):
            raise PluginError("forbidden", "Route does not match this request")
        entry.scopes = (
            *entry.resource.resource.required_scopes,
            *(route.required_scopes if route else ()),
        )
        await self.principal(entry, entry.scopes)
        csrf = self.csrf(entry)
        if entry.method not in SAFE_METHODS:
            if not isinstance(params.get("revision"), str) or not hmac.compare_digest(
                params["revision"], entry.revision
            ):
                raise PluginError(
                    "web_revision_stale",
                    "The page changed; reload before submitting again",
                )
            if not isinstance(params.get("csrf"), str) or not hmac.compare_digest(
                params["csrf"], csrf
            ):
                raise PluginError("csrf_failed", "Form verification failed")
        entry.admitted = True
        presentation = await self.host.access.presentation(entry.identity)
        return {
            "presentation": presentation,
            "revision": entry.revision,
            "csrf": csrf,
            "public_base": entry.public_base,
            "links": await self.links(entry.identity),
        }

    async def links(self, identity):
        """Resolve only available entrances that this visitor may open."""
        links = {}
        for target in self.host.gateway.mounts:
            if target == "provider.dashboard":
                if (
                    self.host.publication.get("dashboard", {}).get("state")
                    != "available"
                ):
                    continue
                try:
                    await self.host.access.admit(identity)
                    links[target] = self.host.gateway.resolve(target)
                except PluginError:
                    pass
            elif target.startswith("web."):
                if self.host.publication.get("web", {}).get("state") != "available":
                    continue
                _, owner, identifier = target.split(".")
                runtime = self.host.runtimes.get(owner)
                if not runtime or not runtime.admitted or owner in self.host.gated:
                    continue
                resource = next(
                    (
                        r
                        for r in self.host.graph.resources()
                        if r.owner == owner
                        and r.resource.id == identifier
                        and r.resource.kind == "web.app"
                    ),
                    None,
                )
                if resource and resource.key in self.catalogs:
                    try:
                        authenticated = (
                            resource.resource.data.get("access") == "authenticated"
                        )
                        actor = await self.host.access.admit(
                            identity,
                            resource.resource.required_scopes,
                            effective=not authenticated,
                        )
                        if not authenticated or actor.kind != "anonymous":
                            links[target] = self.host.gateway.resolve(target)
                    except PluginError:
                        pass
        return links

    async def resolve(self, runtime, params, scopes):
        entry = self.context(
            runtime, params["key"], params["epoch"], params.get("access_context")
        )
        if not entry.admitted or entry.method == "OPTIONS":
            raise PluginError("forbidden", "Web handler was not admitted")
        return await self.principal(entry, (*entry.scopes, *scopes), activity=True)
