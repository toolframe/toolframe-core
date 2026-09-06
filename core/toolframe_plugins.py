"""Toolframe's plugin catalog and shared service access for tools and widgets."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi_cordis import Plugin, PluginError, PluginManager

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


def _read_catalog(app_dir: Path) -> tuple[list[Plugin], dict[str, tuple[str, ...]]]:
    path = app_dir / "plugins.py"
    if not path.exists() and not path.is_symlink():
        return [], {}
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
        return list(plugins), result
    except BaseException:
        sys.modules.pop(name, None)
        raise


def configure_plugins(
    *, app_dir: Path, domains_dir: Path, database: Any, artifacts: Any
) -> PluginManager:
    """Build and validate definitions once, before preflight or serving requests.

    Existing import-time database/artifact helpers remain available. Their handles
    are also exposed as host-owned services in the shared lifecycle graph.
    """
    global _manager
    plugins, bindings = _read_catalog(app_dir)
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
    definitions = [
        Plugin(
            "toolframe.database",
            lambda _: database,
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
                    "toolframe.database",
                    "toolframe.artifacts",
                    *bindings.get(domain, ()),
                ),
                description=f"Tools and widgets in {domain}",
            )
            for domain in domains
        ],
    ]
    manager = PluginManager(definitions)
    with _lock:
        _manager = manager
    return manager
