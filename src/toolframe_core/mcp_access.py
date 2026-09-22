"""MCP-only admission. Protected MCP never inherits Dashboard public fallback."""

import math
import secrets
import time
from dataclasses import dataclass

from toolframe_sdk import PluginError, Principal


@dataclass(frozen=True)
class McpRequest:
    runtime: object
    mode: str
    auth_provider: str
    credential_id: str | None


class McpAccess:
    def __init__(self, host):
        self.host = host
        self.requests: dict[str, McpRequest] = {}

    def status(self):
        policy = self.host.composition.mcp_access
        definition = self.host.graph.definitions.get(policy.auth_provider)
        available = bool(
            definition
            and definition.provider_kind == "mcp-auth"
            and self.host.healthy_runtime(policy.auth_provider)
        )
        return {
            "configured": policy.mode,
            "mode": "unavailable"
            if policy.mode == "protected" and not available
            else policy.mode,
            "auth_provider": policy.auth_provider,
            "provider_available": available,
        }

    async def auth(self, method, params, *, principal=None):
        name = self.host.composition.mcp_access.auth_provider
        definition = self.host.graph.definitions.get(name)
        if (
            not definition
            or definition.provider_kind != "mcp-auth"
            or not self.host.healthy_runtime(name)
        ):
            raise PluginError(
                "auth_unavailable", "MCP authentication provider is unavailable"
            )
        try:
            result = await self.host.call_service(
                name, "toolframe.mcp-auth", method, params, principal=principal
            )
            if not isinstance(result, dict):
                raise TypeError("Invalid MCP auth result")
            if method in {"verify", "resolve"}:
                actor = Principal.model_validate(result["principal"])
                if (
                    actor.kind != "mcp_key"
                    or not actor.credential_id
                    or actor.all_business
                    or any(s.startswith("system.") for s in actor.scopes)
                    or any(
                        self.host.access.catalog().get(s, {}).get("category")
                        == "system"
                        for s in actor.scopes
                    )
                ):
                    raise ValueError("Invalid MCP principal")
                if actor.expires_at is not None:
                    if not math.isfinite(actor.expires_at):
                        raise ValueError("Invalid MCP expiry")
                    if actor.expires_at <= time.time():
                        raise PluginError(
                            "invalid_credentials", "Invalid or expired MCP key"
                        )
            return result
        except PluginError as error:
            if error.code in {
                "invalid_credentials",
                "invalid_request",
                "forbidden",
                "credential_missing",
                "rate_limited",
            }:
                raise
            raise PluginError(
                "auth_unavailable", "MCP authentication provider is unavailable"
            ) from None
        except Exception:  # noqa: BLE001 -- malformed decisions must close MCP access.
            raise PluginError(
                "auth_unavailable", "MCP authentication provider failed"
            ) from None

    async def register(self, runtime, headers):
        policy = self.host.composition.mcp_access
        values = headers.getlist("authorization")
        identifier = None
        if policy.mode == "protected":
            if not self.status()["provider_available"]:
                raise PluginError(
                    "auth_unavailable", "MCP authentication provider is unavailable"
                )
            if not values:
                raise PluginError(
                    "authentication_required", "An MCP bearer key is required"
                )
            if len(values) != 1:
                raise PluginError("invalid_credentials", "Supply one bearer credential")
            scheme, separator, token = values[0].partition(" ")
            if not separator or scheme.lower() != "bearer" or not token:
                raise PluginError("invalid_credentials", "Supply an MCP bearer key")
            result = await self.auth("verify", {"token": token})
            identifier = result["principal"]["credential_id"]
        if len(self.requests) >= 4096:
            raise PluginError("rate_limited", "Too many active MCP requests")
        reference = secrets.token_urlsafe(32)
        self.requests[reference] = McpRequest(
            runtime, policy.mode, policy.auth_provider, identifier
        )
        return reference

    def release(self, reference):
        self.requests.pop(reference, None)

    async def resolve(self, runtime, reference, *, scopes=(), activity=False):
        request = self.requests.get(reference) if isinstance(reference, str) else None
        policy = self.host.composition.mcp_access
        if (
            request is None
            or request.runtime is not runtime
            or self.host.runtimes.get(runtime.name) is not runtime
            or request.mode != policy.mode
            or request.auth_provider != policy.auth_provider
        ):
            raise PluginError(
                "authentication_required", "MCP request context is no longer valid"
            )
        if request.credential_id is None:
            if policy.mode != "public":
                raise PluginError(
                    "authentication_required", "An MCP bearer key is required"
                )
            actor = Principal(kind="public", all_business=True)
        else:
            result = await self.auth(
                "resolve",
                {
                    "id": request.credential_id,
                    "required_scopes": list(scopes),
                    "activity": activity,
                },
            )
            actor = Principal.model_validate(result["principal"])
            if actor.credential_id != request.credential_id:
                raise PluginError(
                    "auth_unavailable",
                    "MCP authentication returned an invalid identity",
                )
        if not actor.allows(scopes, self.host.access.catalog()):
            raise PluginError(
                "forbidden", "MCP key does not grant this tool's permissions"
            )
        return actor

    async def presentation(self, runtime, reference):
        actor = await self.resolve(runtime, reference)
        graph = self.host.graph
        catalog = self.host.access.catalog()
        visible = []
        for owned in graph.resources():
            resource = owned.resource
            if resource.kind != "mcp.tool":
                continue
            service = resource.data["service"]
            definition = graph.definitions[owned.owner]
            if service not in definition.services:
                targets = [
                    owner
                    for owner, name in graph.bindings[owned.owner].values()
                    if name == service
                ]
                if not targets:
                    continue
                definition = graph.definitions[targets[0]]
            method = definition.services[service].methods[resource.data["method"]]
            if method.audience == "business" and actor.allows(
                (*resource.required_scopes, *method.required_scopes), catalog
            ):
                visible.append(owned.key)
        policy = self.host.composition.mcp_access
        return {
            "principal": actor.model_dump(),
            "resources": visible,
            "issuer": f"toolframe:{self.host.composition.application.id}:{policy.auth_provider}",
        }

    async def manage(self, identity, action, params):
        if action == "policy":
            await self.host.access.admit(
                identity, ["system.settings.manage"], effective=False, activity=True
            )
            if not isinstance(params.get("mode"), str) or params["mode"] not in {
                "public",
                "protected",
            }:
                raise PluginError(
                    "invalid_request", "Supply public or protected MCP mode"
                )
            async with self.host.mutation:
                await self.host.access.admit(
                    identity, ["system.settings.manage"], effective=False
                )

                def edit(document):
                    document.setdefault("mcp_access", {})["mode"] = params["mode"]

                self.host.composition, self.host.revision = self.host.config.edit(
                    params.get("expected_revision"), edit
                )
                self.host.write_lock()
            return self.status()
        actor = await self.host.access.admit(
            identity,
            ["system.auth.mcp_keys"],
            effective=False,
            activity=action in {"create", "revoke"},
        )
        if action == "catalog":
            return {
                "scopes": {
                    s: item
                    for s, item in self.host.access.catalog().items()
                    if item["category"] == "business"
                    and actor.allows([s], self.host.access.catalog())
                },
                "policy": self.host.composition.mcp_access.model_dump(),
                "status": self.status(),
                "revision": self.host.config.revision,
                "max_expires_at": actor.expires_at,
                "mcp_path": "/mcp",
            }
        if action not in {"create", "list", "revoke"}:
            raise PluginError("invalid_request", "Unknown MCP key operation")
        # Credentials bypass the configuration/operation journal entirely.
        return await self.auth(action, params, principal=actor)
