"""Small, explicit Cordis-inspired plugin runtime; no JavaScript compatibility."""

from ._runtime import (
    ConfigurationError,
    DependencyError,
    DrainTimeout,
    Plugin,
    PluginContext,
    PluginError,
    PluginManager,
    PluginUnavailable,
    UnknownPlugin,
)
from .fastapi import install, plugin_router, service

__version__ = "0.1.0"
__all__ = [
    "ConfigurationError",
    "DependencyError",
    "DrainTimeout",
    "Plugin",
    "PluginContext",
    "PluginError",
    "PluginManager",
    "PluginUnavailable",
    "UnknownPlugin",
    "install",
    "plugin_router",
    "service",
]
