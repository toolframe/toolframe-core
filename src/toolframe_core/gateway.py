"""Immutable presentation mounts; Core and protocol identities remain stable."""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import urlsplit

from toolframe_sdk import PluginError, digest
from toolframe_sdk.web import public_base

CORE_PREFIXES = ("/_toolframe", "/api", "/oauth", "/.well-known", "/healthz")
PROTOCOLS = {"provider.mcp": "/mcp", "provider.llms": "/llms.txt"}


def contains(prefix, path):
    return prefix == "/" or path == prefix or path.startswith(prefix + "/")


def join(prefix, suffix="/"):
    return prefix.rstrip("/") + "/" + suffix.lstrip("/")


@dataclass(frozen=True)
class Mount:
    target: str
    prefix: str

    def relative(self, path):
        return path[len(self.prefix.rstrip("/")) :] or "/"


@dataclass(frozen=True)
class Gateway:
    profile: str
    revision: str
    mounts: object
    landing_target: str | None

    def match(self, path):
        if any(contains(prefix, path) for prefix in CORE_PREFIXES):
            return None
        if contains("/llms", path):
            return self.mounts.get("provider.llms")
        choices = [item for item in self.mounts.values() if contains(item.prefix, path)]
        return max(choices, key=lambda item: len(item.prefix)) if choices else None

    def resolve(self, target):
        if target not in self.mounts:
            raise PluginError("gateway_target_missing", f"No public mount for {target}")
        return join(self.mounts[target].prefix)

    @property
    def landing_url(self):
        return self.resolve(self.landing_target) if self.landing_target else "/"

    def export(self):
        return {
            "profile": self.profile,
            "revision": self.revision,
            "landing_target": self.landing_target,
            "mounts": {
                key: {"prefix": value.prefix} for key, value in self.mounts.items()
            },
        }


def compile_gateway(composition, graph=None):
    legacy = composition.schema_version == 1
    if legacy and (composition.gateway.mounts or composition.gateway.landing_target):
        raise PluginError(
            "configuration_invalid", "Presentation mounts require schema_version = 2"
        )
    raw = {**PROTOCOLS, **({"provider.dashboard": "/"} if legacy else {})}
    raw.update({key: item.prefix for key, item in composition.gateway.mounts.items()})
    mounts, seen = {}, {}
    for target, prefix in raw.items():
        public_base(prefix)
        if target not in {*PROTOCOLS, "provider.dashboard"}:
            if not re.fullmatch(
                r"web\.[a-z][a-z0-9-]{0,63}\.[a-z][a-z0-9_-]{0,63}", target
            ):
                raise PluginError(
                    "configuration_invalid", f"Unknown gateway target {target}"
                )
            _, owner, identifier = target.split(".")
            if owner not in composition.plugins:
                raise PluginError(
                    "configuration_invalid", f"Mount {target} has no configured owner"
                )
            definition = graph.definitions.get(owner) if graph else None
            if definition is not None:
                resource = next(
                    (
                        r
                        for r in definition.resources
                        if r.kind == "web.app" and r.id == identifier
                    ),
                    None,
                )
                if resource is None:
                    raise PluginError(
                        "configuration_invalid", f"Unknown Web resource {target}"
                    )
                expected = resource.data.get("expected_public_base")
                if expected is not None and prefix != expected:
                    raise PluginError(
                        "configuration_invalid",
                        f"{target} expected_public_base differs from its mount",
                    )
        if target in PROTOCOLS and prefix != PROTOCOLS[target]:
            raise PluginError(
                "configuration_invalid",
                "MCP and LLMS protocol mounts retain their existing URLs",
            )
        if (
            prefix in seen
            or any(contains(core, prefix) for core in CORE_PREFIXES)
            or contains("/llms", prefix)
        ):
            raise PluginError(
                "configuration_invalid",
                f"Gateway prefix {prefix} conflicts with an existing namespace",
            )
        seen[prefix] = target
        mounts[target] = Mount(target, prefix)
    for target, mount in mounts.items():
        if target.startswith("web.") and any(
            other != target and contains(join(mount.prefix, "/_tf"), item.prefix)
            for other, item in mounts.items()
        ):
            raise PluginError(
                "configuration_invalid",
                f"Another mount shadows {target}'s asset namespace",
            )
    landing = composition.gateway.landing_target
    if landing and (
        landing not in mounts or not landing.startswith(("web.", "provider.dashboard"))
    ):
        raise PluginError(
            "configuration_invalid",
            "landing_target must reference a mounted presentation",
        )
    landing = landing or next(
        (target for target, item in mounts.items() if item.prefix == "/"), None
    )
    payload = {
        "profile": "legacy-v1" if legacy else "gateway-v2",
        "mounts": raw,
        "landing_target": landing,
    }
    return Gateway(
        payload["profile"], digest(payload), MappingProxyType(mounts), landing
    )


def validate_web_catalogs(gateway, resources, catalogs, graph):
    def shadowed(route_path, prefix):
        route_parts, prefix_parts = (
            route_path.strip("/").split("/"),
            prefix.strip("/").split("/"),
        )
        return len(prefix_parts) <= len(route_parts) and all(
            a == b or (a.startswith("{") and a.endswith("}"))
            for a, b in zip(route_parts, prefix_parts)
        )

    for resource in resources:
        catalog = catalogs[resource.key]
        declared = graph.definitions[resource.owner].authorization.scopes
        for route in catalog.routes:
            if any(scope not in declared for scope in route.required_scopes):
                raise PluginError(
                    "resource_target_invalid",
                    "Web routes must use scopes declared by their owner",
                )
        target = f"web.{resource.owner}.{resource.resource.id}"
        mount = gateway.mounts.get(target)
        if mount is None:
            continue
        for route in catalog.routes:
            full = join(mount.prefix, route.path)
            for other, item in gateway.mounts.items():
                if other == target or not contains(mount.prefix, item.prefix):
                    continue
                relative = mount.relative(item.prefix)
                # A concrete mount landing cannot hide a declared exact or parameter route.
                if (
                    contains(item.prefix, full)
                    or shadowed(full, item.prefix)
                    or route.match(relative) is not None
                    or route.match(relative + "/") is not None
                ):
                    raise PluginError(
                        "configuration_invalid",
                        f"{target}/{route.id} is shadowed by {other}",
                    )
            if any(
                contains(core, full) or shadowed(full, core) for core in CORE_PREFIXES
            ):
                raise PluginError(
                    "configuration_invalid",
                    f"{target}/{route.id} shadows a Core namespace",
                )


def endpoints(gateway, kind=None):
    target = "provider." + str(kind)
    mount = gateway.mounts.get(target)
    return {
        "profile": gateway.profile,
        "revision": gateway.revision,
        "public_base": mount.prefix if mount else "/",
        "login_url": "/_toolframe/auth/login",
        "logout_url": "/_toolframe/auth/logout",
        "session_url": "/_toolframe/session",
        "oidc_providers_url": "/_toolframe/oidc/providers",
        "landing_url": gateway.landing_url,
    }


def return_path(value, gateway):
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
    ):
        return gateway.landing_url
    uri = urlsplit(value)
    if (
        uri.scheme
        or uri.netloc
        or uri.fragment
        or any(c in value for c in "\\\r\n\x00")
        or "%" in uri.path
        or any(part in {".", ".."} for part in uri.path.split("/"))
    ):
        return gateway.landing_url
    if (
        any(contains(prefix, uri.path) for prefix in CORE_PREFIXES)
        or gateway.match(uri.path) is None
    ):
        return gateway.landing_url
    return value
