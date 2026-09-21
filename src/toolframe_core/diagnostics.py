"""Read-only topology from selected definitions and the lifecycle planner.

Never import plugin code or prepare candidates here. Inactive declarations are
useful for diagnosis, but must not replace the running graph with a newer wheel.
"""

from toolframe_sdk import PluginDefinition

from .graph import Graph

PRESENTATIONS = {
    "dashboard.page": "dashboard",
    "mcp.tool": "mcp",
    "llms.document": "llms",
}


def dependency_snapshot(graph, plugins, candidates, bindings, cordis, publication):
    definitions = {
        name: PluginDefinition.model_validate(candidate["definition"])
        for name, candidate in candidates.items()
        if name not in graph.definitions
    }
    definitions.update(graph.definitions)
    lifecycle = {item["name"]: item for item in cordis}
    nodes, edges = [], []
    for item in plugins:
        name = item["id"]
        definition = definitions.get(name)
        selected = name in graph.definitions
        observed = lifecycle.get(name, {})
        state = observed.get("state", item["runtime"].get("state", "installed"))
        if not selected:
            if item["desired"].get("installed") is False:
                state = "removed"
            elif item["stopped_override"]:
                state = "stopped"
            elif not item["desired"].get("enabled", False):
                state = "disabled"
        candidate = graph.candidates.get(name, candidates.get(name, {}))
        nodes.append(
            {
                "id": name,
                "label": name,
                "kind": definition.kind if definition else "unknown",
                "provider_kind": definition.provider_kind if definition else None,
                "version": candidate.get("version"),
                "state": state,
                "healthy": item["healthy"],
                "health_fresh": item["health_fresh"],
                "enabled": item["desired"].get("enabled", False),
                "selected": selected,
                "definition_source": "selected"
                if selected
                else "last_candidate"
                if definition
                else "unavailable",
                "in_flight": observed.get("inFlight", 0),
                "blocked_by": observed.get("blockedBy", item["blocked_by"]),
            }
        )
        if not definition:
            continue
        for alias, requirement in sorted(definition.requires.items()):
            resolved = graph.bindings.get(name, {}).get(alias) if selected else None
            # For an unresolved/inactive declaration, show a known supplier only
            # if unambiguous. This is not a new runtime binding.
            explicit = bindings.get(
                f"{name}/{alias}", bindings.get(requirement.service)
            )
            matches = [
                owner
                for owner, target in definitions.items()
                if requirement.service in target.services
                and target.services[requirement.service].version == requirement.version
                and (not explicit or owner == explicit)
            ]
            target = (
                resolved[0] if resolved else matches[0] if len(matches) == 1 else None
            )
            state = "resolved" if resolved else "missing" if selected else "inactive"
            if target is None:
                target = f"requirement:{name}:{alias}"
                nodes.append(
                    {
                        "id": target,
                        "label": f"{requirement.service} v{requirement.version}",
                        "kind": "unresolved",
                        "state": "ambiguous" if len(matches) > 1 else "missing",
                    }
                )
            edges.append(
                {
                    "source": name,
                    "target": target,
                    "kind": "optional" if requirement.optional else "required",
                    "label": f"{requirement.service} v{requirement.version}",
                    "alias": alias,
                    "state": state,
                }
            )
        for resource_kind, provider_kind in PRESENTATIONS.items():
            resources = sorted(
                resource.id
                for resource in definition.resources
                if resource.kind == resource_kind
            )
            if not resources:
                continue
            target = graph.providers.get(provider_kind)
            if target is None:
                matches = [
                    owner
                    for owner, target_definition in definitions.items()
                    if target_definition.provider_kind == provider_kind
                ]
                target = (
                    matches[0] if len(matches) == 1 else f"provider:{provider_kind}"
                )
                if target.startswith("provider:") and not any(
                    node["id"] == target for node in nodes
                ):
                    nodes.append(
                        {
                            "id": target,
                            "label": f"{provider_kind} provider",
                            "kind": "unresolved",
                            "state": "missing",
                        }
                    )
            edges.append(
                {
                    "source": name,
                    "target": target,
                    "kind": "presentation",
                    "label": resource_kind,
                    "resources": resources,
                    "state": "inactive"
                    if not selected
                    else "resolved"
                    if target in graph.definitions
                    else "missing",
                    "publication": publication.get(provider_kind, {}).get(
                        "state", "unavailable"
                    ),
                }
            )
    impacts = {}
    for name in sorted(graph.definitions):
        impacts[name] = {}
        for action in ("reload", "disable"):
            replacement = (
                graph
                if action == "reload"
                else Graph(
                    {
                        key: value
                        for key, value in graph.candidates.items()
                        if key != name
                    },
                    graph.binding_preferences,
                )
            )
            affected = graph.affected(replacement, {name})
            dependents = graph.dependents({name}) - {name}
            presentations = [
                {
                    "owner": edge["source"],
                    "provider": edge["target"],
                    "kind": edge["label"],
                    "resources": edge["resources"],
                }
                for edge in edges
                if edge["kind"] == "presentation"
                and edge["source"] in graph.definitions
                and (edge["source"] in affected or edge["target"] in affected)
            ]
            impacts[name][action] = {
                "affected": sorted(affected),
                "required_dependents": sorted(dependents),
                "presentation_providers": sorted(affected - dependents - {name}),
                "optional_consumers": sorted(
                    owner
                    for owner, resolved in graph.bindings.items()
                    if owner not in affected
                    and any(
                        target in affected
                        and graph.definitions[owner].requires[alias].optional
                        for alias, (target, _) in resolved.items()
                    )
                ),
                "presentations": presentations,
            }
    return {
        "schema_version": 1,
        "basis": "selected",
        "nodes": sorted(nodes, key=lambda node: node["id"]),
        "edges": edges,
        "impacts": impacts,
    }
