"""Public lifecycle errors shared by the runtime and resource scopes."""


class PluginError(RuntimeError):
    """A plugin operation could not complete."""


class UnknownPlugin(PluginError):
    pass


class ConfigurationError(PluginError, ValueError):
    """Configuration was rejected before changing any running services."""


class DependencyError(PluginError):
    pass


class DrainTimeout(PluginError):
    """Work is still using a service; its resources have NOT been disposed."""


class PluginUnavailable(PluginError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"Plugin {name!r} is not accepting work")
