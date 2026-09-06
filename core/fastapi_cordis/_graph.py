"""Pure dependency planning: validate definitions before changing live resources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ._errors import DependencyError, UnknownPlugin
from ._services import ServiceKey

if TYPE_CHECKING:
    from ._runtime import Plugin


def provided_keys(plugin: Plugin) -> tuple[ServiceKey, ...]:
    if plugin.provides is None:
        return ()
    if isinstance(plugin.provides, ServiceKey):
        return (plugin.provides,)
    return plugin.provides


def reference_name(reference: str | ServiceKey) -> str:
    return reference.name if isinstance(reference, ServiceKey) else reference


def qualify(scope: str, name: str) -> str:
    return f"{scope}/{name}" if scope else name


def candidates(scope: str, name: str):
    if "/" in name:
        yield name.removeprefix("/")
        return
    while scope:
        yield qualify(scope, name)
        scope = scope.rpartition(".")[0]
    yield name


def resolve_provider(
    providers: dict[str, str],
    plugins: dict[str, Plugin],
    reference: str | ServiceKey,
    scope: str = "",
) -> str:
    name = reference_name(reference)
    for candidate in candidates(scope, name):
        if candidate not in providers:
            continue
        provider = providers[candidate]
        if isinstance(reference, ServiceKey) and reference not in provided_keys(
            plugins[provider]
        ):
            raise DependencyError(f"Incompatible contract for service {name!r}")
        return provider
    raise UnknownPlugin(next(candidates(scope, name)))


@dataclass(frozen=True)
class DependencyGraph:
    providers: dict[str, str]
    dependencies: dict[str, tuple[str, ...]]
    missing: dict[str, tuple[str, ...]]
    order: tuple[str, ...]


def build_graph(plugins: dict[str, Plugin]) -> DependencyGraph:
    providers = {name: name for name in plugins}
    for name, plugin in plugins.items():
        for reference in provided_keys(plugin):
            key = qualify(plugin.scope, reference.name)
            if key in providers and providers[key] != name:
                raise DependencyError(f"Multiple providers for service {key!r}")
            providers[key] = name
    dependencies: dict[str, tuple[str, ...]] = {}
    missing: dict[str, tuple[str, ...]] = {}
    for name, plugin in plugins.items():
        resolved = []
        absent = []
        for reference in plugin.requires:
            try:
                resolved.append(
                    resolve_provider(providers, plugins, reference, plugin.scope)
                )
            except UnknownPlugin as error:
                absent.append(error.args[0])
        dependencies[name] = tuple(dict.fromkeys(resolved))
        missing[name] = tuple(dict.fromkeys(absent))

    ordered = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            raise DependencyError(f"Dependency cycle at {name!r}")
        if name in visited:
            return
        visiting.add(name)
        for dependency in dependencies[name]:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)
        ordered.append(name)

    for name in plugins:
        visit(name)
    return DependencyGraph(providers, dependencies, missing, tuple(ordered))
