"""Toolframe's plugin catalog and shared service access for tools and widgets."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
import threading
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import anyio
from fastapi_cordis import Plugin, PluginError, PluginManager
from toolframe_database_service import DATABASE, DatabaseProvider
from toolframe_migrations import MigrationCoordinator, Schema, discover_domains

_manager: PluginManager | None = None
_lock = threading.RLock()


def get_plugins() -> PluginManager:
    """Return the process manager; callers use .lease(name) around actual work."""
    with _lock:
        if _manager is None:
            raise PluginError("Toolframe plugins have not been configured")
        return _manager


def use_plugin(name: str):
    """Lease a service in a synchronous MCP tool or Streamlit worker thread."""
    return get_plugins().lease(name)


def plugin_unavailable_details(manager: PluginManager, name: str) -> dict[str, Any]:
    """Describe a recovery action using public state, without reading configuration."""
    snapshot = {item["name"]: item for item in manager.snapshot()}
    item = snapshot.get(name, {})
    state = item.get("state", "unknown")
    blocked = item.get("blockedBy", [])
    if state == "failed":
        hint = f"Inspect the server log, correct the cause, then Retry {name} at /info."
    elif state == "blocked":
        hint = (
            f"Restore dependencies ({', '.join(blocked)}) at /info; "
            f"{name} will resume automatically. Failed dependencies require Retry."
        )
    elif state == "disabled":
        hint = f"Enable {name} at /info before retrying."
    else:
        hint = "Check /api/plugins for current states and recovery actions at /info."
    return {"plugin": name, "state": state, "blockedBy": blocked, "recoveryHint": hint}


def _read_catalog(
    app_dir: Path,
) -> tuple[list[Plugin], dict[str, tuple[str, ...]], dict[str, Schema]]:
    path = app_dir / "plugins.py"
    if not path.exists() and not path.is_symlink():
        return [], {}, {}
    if not path.is_file() or path.is_symlink():
        raise PluginError(f"Plugin catalog must be a regular, non-symlink file: {path}")
    name = (
        "toolframe_app_plugins_" + hashlib.sha256(str(path).encode()).hexdigest()[:16]
    )
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None:
        raise PluginError(f"Cannot load plugin catalog: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        # Compile the current source instead of using a timestamp-based pyc cache.
        exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)  # noqa: S102 - trusted local catalog, like domain modules.
        plugins = getattr(module, "PLUGINS", None)
        bindings = getattr(module, "DOMAIN_PLUGINS", {})
        schemas = getattr(module, "SCHEMAS", {})
        if not isinstance(plugins, (tuple, list)) or not all(
            isinstance(p, Plugin) for p in plugins
        ):
            raise PluginError(
                "app/plugins.py must export PLUGINS as a list or tuple of Plugin definitions"
            )
        if not isinstance(bindings, Mapping):
            raise PluginError(
                "DOMAIN_PLUGINS must map domain names to plugin name sequences"
            )
        result = {}
        for domain, dependencies in bindings.items():
            if (
                not isinstance(domain, str)
                or not isinstance(dependencies, (tuple, list))
                or not all(isinstance(item, str) for item in dependencies)
            ):
                raise PluginError(
                    "DOMAIN_PLUGINS must map domain names to plugin name sequences"
                )
            result[domain] = tuple(dependencies)
        if not isinstance(schemas, dict) or not all(
            isinstance(name, str) and isinstance(schema, Schema)
            for name, schema in schemas.items()
        ):
            raise PluginError("SCHEMAS must map plugin names to Schema declarations")
        if set(schemas) - {plugin.id for plugin in plugins}:
            raise PluginError(
                "SCHEMAS contains a plugin that is not installed in PLUGINS"
            )
        return list(plugins), result, schemas
    except BaseException:
        sys.modules.pop(name, None)
        raise


class ToolframePlugins(PluginManager):
    """The lifecycle graph and the schema coordinator owned by this application."""

    def __init__(self, definitions, migrations, migration_state):
        super().__init__(definitions)
        self.validate()
        self.migrations = migrations
        self.migration_state = migration_state


def configure_plugins(
    *, app_dir: Path, domains_dir: Path, database: Any, artifacts: Any
) -> PluginManager:
    """Build and validate definitions once, before preflight or serving requests.

    Existing import-time database/artifact helpers remain available. Their handles
    are also exposed as host-owned services in the shared lifecycle graph.
    """
    global _manager
    plugins, bindings, plugin_schemas = _read_catalog(app_dir)
    provider = getattr(database, "_backend", database)
    if not isinstance(provider, DatabaseProvider):
        raise PluginError(
            "Toolframe requires a database provider implementing DatabaseProvider"
        )
    for plugin in plugins:
        if plugin.name.startswith(("toolframe.", "domain.")):
            raise PluginError(f"Plugin name is reserved by Toolframe: {plugin.name!r}")
    domains = sorted(
        path.name
        for path in domains_dir.iterdir()
        if path.is_dir() and not path.name.startswith((".", "_"))
    )
    if set(bindings) - set(domains):
        raise PluginError(
            f"Unknown domains in DOMAIN_PLUGINS: {sorted(set(bindings) - set(domains))}"
        )
    domain_schemas, legacy = discover_domains(domains_dir)
    migrations = MigrationCoordinator(
        provider, [*domain_schemas, *plugin_schemas.values()], legacy=legacy
    )
    migration_state = {}

    async def setup_database(context):
        context.on_dispose(anyio.to_thread.run_sync, provider.close)
        report = await anyio.to_thread.run_sync(migrations.apply)
        migration_state.update(report)
        activate = getattr(database, "_activate", None)
        if activate is not None:
            activate()
        return database

    plugins = [
        replace(plugin, requires=(*plugin.requires, DATABASE))
        if plugin.id in plugin_schemas
        and DATABASE not in plugin.requires
        and DATABASE.name not in plugin.requires
        else plugin
        for plugin in plugins
    ]
    definitions = [
        Plugin(
            f"toolframe.database.{provider.dialect}",
            setup_database,
            provides=DATABASE,
            managed=False,
            description="Host-owned database access",
        ),
        Plugin(
            "toolframe.artifacts",
            lambda _: artifacts,
            managed=False,
            description="Host-owned artifact storage",
        ),
        *plugins,
        *[
            Plugin(
                f"domain.{domain}",
                lambda _: None,
                requires=(
                    DATABASE,
                    "toolframe.artifacts",
                    *bindings.get(domain, ()),
                ),
                description=f"Tools and widgets in {domain}",
            )
            for domain in domains
        ],
    ]
    manager = ToolframePlugins(definitions, migrations, migration_state)
    with _lock:
        _manager = manager
    return manager
