"""Small, explicit Cordis-inspired plugin runtime; no JavaScript compatibility."""

from ._events import EventKey
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
from ._scope import EffectScope
from ._services import ServiceKey
from .fastapi import install, plugin_router, service

__version__ = "0.2.0"
__all__ = [
    "ConfigurationError",
    "DependencyError",
    "DrainTimeout",
    "EffectScope",
    "EventKey",
    "Plugin",
    "PluginContext",
    "PluginError",
    "PluginManager",
    "PluginUnavailable",
    "ServiceKey",
    "UnknownPlugin",
    "install",
    "plugin_router",
    "service",
]
