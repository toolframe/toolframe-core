"""Resolve explicit service identities independently of presentation attachment."""

from graphlib import CycleError, TopologicalSorter

from toolframe_sdk import OwnedResource, PluginDefinition, PluginError, digest


class Graph:
    def __init__(self, candidates, bindings):
        self.candidates = candidates
        self.binding_preferences = dict(bindings)
        self.definitions = {
            name: PluginDefinition.model_validate(value["definition"])
            for name, value in candidates.items()
        }
        self.dependencies = {name: set() for name in candidates}
        self.bindings = {}
        self.missing = {}
        self.providers = {}
        self.storage_providers = set()
        self.stores = {}
        for name, definition in self.definitions.items():
            if definition.provider_kind == "storage":
                self.storage_providers.add(name)
            elif definition.provider_kind:
                if definition.provider_kind in self.providers:
                    raise PluginError(
                        "provider_conflict",
                        f"Only one enabled {definition.provider_kind} provider can own its routes/store",
                    )
                self.providers[definition.provider_kind] = name
        for name, definition in self.definitions.items():
            resolved = self.bindings[name] = {}
            missing = self.missing[name] = []
            for alias, requirement in definition.requires.items():
                matches = [
                    owner
                    for owner, target in self.definitions.items()
                    if requirement.service in target.services
                    and target.services[requirement.service].version
                    == requirement.version
                ]
                explicit = bindings.get(
                    f"{name}/{alias}", bindings.get(requirement.service)
                )
                if explicit:
                    matches = [owner for owner in matches if owner == explicit]
                if len(matches) > 1:
                    raise PluginError(
                        "service_ambiguous",
                        f"Bind {name}/{alias} explicitly; several plugins provide {requirement.service}",
                    )
                if not matches:
                    if not requirement.optional:
                        missing.append(requirement.service)
                    continue
                resolved[alias] = (matches[0], requirement.service)
                if not requirement.optional:
                    self.dependencies[name].add(matches[0])
            sql_requirements = {
                alias: requirement
                for alias, requirement in definition.requires.items()
                if requirement.service == "toolframe.storage.sql"
            }
            if definition.store and not any(
                not requirement.optional for requirement in sql_requirements.values()
            ):
                raise PluginError(
                    "invalid_store_dependency",
                    "A managed store requires a non-optional SQL storage dependency",
                    {"owner": name},
                )
            storage = {
                resolved[alias][0] for alias in sql_requirements if alias in resolved
            }
            if len(storage) > 1:
                raise PluginError(
                    "multiple_store_providers",
                    "Each plugin may bind its SQL store to only one provider",
                    {"owner": name, "providers": sorted(storage)},
                )
            if storage:
                provider = next(iter(storage))
                if provider not in self.storage_providers:
                    raise PluginError(
                        "invalid_storage_provider",
                        "The SQL service must be supplied by a storage provider",
                        {"owner": name, "provider": provider},
                    )
                self.stores[name] = provider
            for resource in definition.resources:
                if resource.kind == "mcp.tool":
                    service = resource.data["service"]
                    if service in definition.services:
                        target = definition.services[service]
                    else:
                        matching = [
                            alias
                            for alias, item in definition.requires.items()
                            if item.service == service
                        ]
                        if not matching:
                            raise PluginError(
                                "resource_target_invalid",
                                f"{name}/{resource.id} targets an undeclared service",
                            )
                        resolution = resolved.get(matching[0])
                        target = (
                            self.definitions[resolution[0]].services[service]
                            if resolution
                            else None
                        )
                    if target and resource.data["method"] not in target.methods:
                        raise PluginError(
                            "resource_target_invalid",
                            f"{name}/{resource.id} targets an unknown method",
                        )
                    if (
                        target
                        and target.methods[resource.data["method"]].audience
                        != "business"
                    ):
                        raise PluginError(
                            "resource_target_invalid",
                            "Public MCP tools must target business methods",
                        )
        try:
            self.order = tuple(TopologicalSorter(self.dependencies).static_order())
        except CycleError as error:
            raise PluginError(
                "dependency_cycle", "Required service dependencies contain a cycle"
            ) from error

    def closure(self, name, *, optional=False, available=None):
        result = set()

        def visit(owner):
            if owner in result:
                return
            result.add(owner)
            for dependency in self.dependencies.get(owner, ()):
                visit(dependency)
            if optional:
                for dependency, _ in self.bindings.get(owner, {}).values():
                    if available is None or dependency in available:
                        visit(dependency)

        visit(name)
        return result

    def dependents(self, names):
        result = set(names)
        for name in self.order:
            if self.dependencies[name] & result:
                result.add(name)
        return result

    def resources(self, generations=None):
        generations = generations or {}
        return [
            OwnedResource(
                owner=name,
                generation=generations.get(name, "candidate"),
                resource=resource,
                content=self.candidates[name].get("documents", {}).get(resource.id)
                if resource.kind == "llms.document"
                else None,
            )
            for name in sorted(self.definitions)
            for resource in self.definitions[name].resources
        ]

    def descriptor_digest(self, kind):
        return digest(
            [
                {
                    "owner": resource.owner,
                    "resource": resource.resource.model_dump(),
                    "content": resource.content,
                }
                for resource in self.resources()
                if resource.resource.kind == kind
            ]
        )

    def affected(self, replacement, changed):
        result = self.dependents(changed) | replacement.dependents(changed)
        page_owners = {
            resource.owner
            for resource in self.resources() + replacement.resources()
            if resource.resource.kind == "dashboard.page"
        }
        if result & page_owners:
            result |= {
                graph.providers["dashboard"]
                for graph in (self, replacement)
                if "dashboard" in graph.providers
            }
        if self.descriptor_digest("mcp.tool") != replacement.descriptor_digest(
            "mcp.tool"
        ):
            result |= {
                graph.providers["mcp"]
                for graph in (self, replacement)
                if "mcp" in graph.providers
            }
        return result
