"""Async lifecycles with thread-safe leases and explicit dependency ownership."""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import math
import re
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel


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


@dataclass(frozen=True)
class Plugin:
    """One named service. Setup returns its value and registers cleanup on ctx."""

    name: str
    setup: Callable[[PluginContext], Any]
    requires: tuple[str, ...] = ()
    config: Mapping[str, Any] = field(default_factory=dict)
    config_model: type[BaseModel] | None = None
    enabled: bool = True
    managed: bool = True
    description: str = ""


class PluginContext:
    def __init__(
        self, manager: PluginManager, plugin: Plugin, config: Any, stack: AsyncExitStack
    ) -> None:
        self.name = plugin.name
        self.config = config
        self._manager = manager
        self._requires = plugin.requires
        self._stack = stack
        self._accepting = True

    def require(self, name: str) -> Any:
        """Get a declared dependency during setup, owned until this plugin stops."""
        if not self._accepting:
            raise PluginError(
                "require() is only available during setup; use a lease at call time"
            )
        if name not in self._requires:
            raise DependencyError(f"{self.name!r} did not declare dependency {name!r}")
        return self._manager._values[name]

    def on_dispose(
        self, callback: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> None:
        """Register sync or async cleanup immediately after acquiring a resource."""
        if not self._accepting:
            raise PluginError("Cleanup must be registered during setup")

        async def cleanup() -> None:
            result = callback(*args, **kwargs)
            if inspect.isawaitable(result):
                await result

        self._stack.push_async_callback(cleanup)


class PluginManager:
    """One asyncio loop owns mutations; any thread may hold synchronous leases.

    Services must only be used within a lease, or by a declared dependent plugin.
    Mutations finish even when the caller is cancelled. No source-code reload,
    dependency installation, subprocess isolation, or multi-worker coordination.
    """

    def __init__(
        self, plugins: Sequence[Plugin] = (), *, drain_timeout: float = 30.0
    ) -> None:
        if not math.isfinite(drain_timeout) or drain_timeout <= 0:
            raise ValueError("drain_timeout must be a positive finite number")
        self.drain_timeout = drain_timeout
        self._plugins: dict[str, Plugin] = {}
        self._configs: dict[str, dict[str, Any]] = {}
        self._enabled: dict[str, bool] = {}
        self._state: dict[str, str] = {}
        self._errors: dict[str, str | None] = {}
        self._values: dict[str, Any] = {}
        self._stacks: dict[str, AsyncExitStack] = {}
        self._counts: dict[str, int] = {}
        self._guard = threading.RLock()
        self._lock = asyncio.Lock()
        self._changed = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        for plugin in plugins:
            if not isinstance(plugin, Plugin):
                raise TypeError("Expected Plugin definitions")
            if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]*", plugin.name):
                raise ValueError(f"Invalid plugin name: {plugin.name!r}")
            if plugin.name in self._plugins:
                raise DependencyError(f"Duplicate plugin: {plugin.name!r}")
            if not callable(plugin.setup):
                raise TypeError(f"Plugin {plugin.name!r} setup must be callable")
            if not isinstance(plugin.requires, tuple) or not all(
                isinstance(n, str) for n in plugin.requires
            ):
                raise TypeError("Plugin requires must be a tuple of names")
            self._plugins[plugin.name] = plugin
            self._configs[plugin.name] = self._validate_config(plugin, plugin.config)
            self._enabled[plugin.name] = plugin.enabled
            self._state[plugin.name] = "registered" if plugin.enabled else "disabled"
            self._errors[plugin.name] = None
            self._counts[plugin.name] = 0
        self._order = self._topological_order()
        self.validate()

    def _topological_order(self) -> tuple[str, ...]:
        ordered: list[str] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name not in self._plugins:
                raise DependencyError(f"Missing plugin dependency: {name!r}")
            if name in visiting:
                raise DependencyError(f"Dependency cycle at {name!r}")
            if name in visited:
                return
            visiting.add(name)
            for dependency in self._plugins[name].requires:
                visit(dependency)
            visiting.remove(name)
            visited.add(name)
            ordered.append(name)

        for name in self._plugins:
            visit(name)
        return tuple(ordered)

    @staticmethod
    def _validate_config(plugin: Plugin, config: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(config, Mapping) or not all(
            isinstance(key, str) for key in config
        ):
            raise ConfigurationError(
                "Plugin config must be a JSON object with string keys"
            )
        try:
            detached = json.loads(json.dumps(dict(config), allow_nan=False))
            if plugin.config_model is not None:
                plugin.config_model.model_validate(detached)
        except (ValueError, TypeError) as error:
            raise ConfigurationError(
                f"Invalid configuration for {plugin.name!r}"
            ) from error
        return detached

    def validate(self) -> None:
        """Validate enabled dependencies without executing plugin setup."""
        for name in self._order:
            if self._enabled[name]:
                for dependency in self._plugins[name].requires:
                    if not self._enabled[dependency]:
                        raise DependencyError(
                            f"Enabled plugin {name!r} requires disabled {dependency!r}"
                        )

    def snapshot(self) -> list[dict[str, Any]]:
        """Return detached diagnostics. Configuration and service values stay private."""
        with self._guard:
            return [
                {
                    "name": name,
                    "description": self._plugins[name].description,
                    "requires": list(self._plugins[name].requires),
                    "enabled": self._enabled[name],
                    "managed": self._plugins[name].managed,
                    "state": self._state[name],
                    "inFlight": self._counts[name],
                    "error": self._errors[name],
                }
                for name in self._order
            ]

    def _ancestors(self, name: str) -> set[str]:
        self._lookup(name)
        names = {name}
        for dependency in self._plugins[name].requires:
            names.update(self._ancestors(dependency))
        return names

    @contextmanager
    def lease(self, name: str) -> Iterator[Any]:
        """Hold a service and all its dependencies until actual work has finished."""
        names = self._ancestors(name)
        with self._guard:
            if any(self._state[item] != "active" for item in names):
                raise PluginUnavailable(name)
            for item in names:
                self._counts[item] += 1
            value = self._values[name]
        try:
            yield value
        finally:
            with self._guard:
                for item in names:
                    self._counts[item] -= 1
                if self._loop is not None and not self._loop.is_closed():
                    self._loop.call_soon_threadsafe(self._changed.set)

    def _lookup(self, name: str, *, managed: bool = False) -> Plugin:
        if name not in self._plugins:
            raise UnknownPlugin(name)
        plugin = self._plugins[name]
        if managed and not plugin.managed:
            raise PluginError(f"Plugin {name!r} is owned by the host")
        return plugin

    async def _atomic(self, operation: Callable[[], Any]) -> Any:
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise PluginError("PluginManager belongs to another event loop")
        self._loop = loop

        async def serialized() -> Any:
            async with self._lock:
                return await operation()

        task = asyncio.create_task(serialized())
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except BaseException:  # noqa: BLE001 - task.result() propagates the original failure below.
                break
        if cancelled:
            # Retrieve any failure so an abandoned operation cannot emit a warning.
            if not task.cancelled():
                task.exception()
            raise asyncio.CancelledError
        return task.result()

    async def _activate(self, name: str, *, publish: bool = True) -> None:
        plugin = self._plugins[name]
        if any(
            self._state[dep] not in {"active", "starting"} or dep not in self._values
            for dep in plugin.requires
        ):
            raise DependencyError(f"Dependencies of {name!r} are not active")
        stack = AsyncExitStack()
        config = copy.deepcopy(self._configs[name])
        if plugin.config_model is not None:
            config = plugin.config_model.model_validate(config)
        context = PluginContext(self, plugin, config, stack)
        with self._guard:
            self._state[name] = "starting"
            self._errors[name] = None
        try:
            value = plugin.setup(context)
            if inspect.isawaitable(value):
                value = await value
        except BaseException as error:
            context._accepting = False
            with self._guard:
                self._state[name] = "failed"
                self._errors[name] = "setup_failed"
            try:
                await stack.aclose()
            except BaseException as cleanup_error:  # noqa: BLE001 - preserve both failures in the group.
                raise BaseExceptionGroup(
                    "Plugin setup and cleanup failed", [error, cleanup_error]
                )
            raise
        finally:
            context._accepting = False
        with self._guard:
            self._stacks[name] = stack
            self._values[name] = value
            self._state[name] = "active" if publish else "starting"

    async def _dispose(self, names: set[str]) -> None:
        errors: list[BaseException] = []
        for name in reversed(self._order):
            if name not in names:
                continue
            stack = self._stacks.pop(name, None)
            with self._guard:
                self._values.pop(name, None)
                self._state[name] = "stopped"
            if stack is not None:
                try:
                    await stack.aclose()
                except BaseException as error:  # noqa: BLE001 - attempt every cleanup, then raise the group.
                    with self._guard:
                        self._state[name] = "failed"
                        self._errors[name] = "cleanup_failed"
                    errors.append(error)
        if errors:
            raise BaseExceptionGroup("Plugin cleanup failed", errors)

    def _active_dependents(self, name: str) -> set[str]:
        return {
            item
            for item in self._order
            if self._state[item] == "active" and name in self._ancestors(item)
        }

    async def _drain(self, names: set[str]) -> None:
        with self._guard:
            previous = {name: self._state[name] for name in names}
            for name in names:
                self._state[name] = "draining"
        try:
            async with asyncio.timeout(self.drain_timeout):
                while True:
                    self._changed.clear()
                    with self._guard:
                        if all(self._counts[name] == 0 for name in names):
                            return
                    await self._changed.wait()
        except BaseException as error:
            with self._guard:
                self._state.update(previous)
            if isinstance(error, TimeoutError):
                raise DrainTimeout(
                    "Plugins are still in use; no resources were disposed"
                ) from error
            raise

    def _require_running(self) -> None:
        if not self._running:
            raise PluginError("PluginManager has not started")

    async def start(self) -> None:
        async def operation() -> None:
            if self._running:
                return
            self.validate()
            started: set[str] = set()
            try:
                for name in self._order:
                    if self._enabled[name]:
                        await self._activate(name, publish=False)
                        started.add(name)
            except BaseException as error:
                await self._drain(started)
                try:
                    await self._dispose(started)
                except BaseException as cleanup_error:  # noqa: BLE001 - preserve startup and cleanup failures.
                    raise BaseExceptionGroup(
                        "Startup and cleanup failed", [error, cleanup_error]
                    )
                raise
            with self._guard:
                for name in started:
                    self._state[name] = "active"
                self._running = True

        await self._atomic(operation)

    async def close(self) -> None:
        async def operation() -> None:
            names = set(self._stacks)
            await self._drain(names)
            try:
                await self._dispose(names)
            finally:
                self._running = False

        await self._atomic(operation)

    @asynccontextmanager
    async def lifespan(self):
        try:
            await self.start()
            yield self
        finally:
            await self.close()

    async def enable(self, name: str) -> None:
        async def operation() -> None:
            self._require_running()
            self._lookup(name, managed=True)
            if self._state[name] != "active":
                await self._activate(name)
            with self._guard:
                self._enabled[name] = True

        await self._atomic(operation)

    async def disable(self, name: str, *, cascade: bool = False) -> None:
        async def operation() -> None:
            self._require_running()
            self._lookup(name, managed=True)
            names = self._active_dependents(name)
            if names - {name} and not cascade:
                raise DependencyError(
                    f"Active dependents: {', '.join(sorted(names - {name}))}"
                )
            for item in names:
                self._lookup(item, managed=True)
            await self._drain(names)
            try:
                await self._dispose(names)
            finally:
                with self._guard:
                    for item in names | {name}:
                        self._enabled[item] = False
                        if self._state[item] != "failed":
                            self._state[item] = "disabled"

        await self._atomic(operation)

    async def configure(self, name: str, config: Mapping[str, Any]) -> None:
        """Replace config and restart active dependents. Failed setup stays stopped.

        External side effects cannot be rolled back. The new config is retained so
        operators can correct it and explicitly enable the affected plugins.
        """
        # Copy at the call boundary so callers cannot mutate a queued config.
        config = self._validate_config(self._lookup(name, managed=True), config)

        async def operation() -> None:
            self._require_running()
            names = self._active_dependents(name)
            for item in names:
                self._lookup(item, managed=True)
            await self._drain(names)
            await self._dispose(names)
            self._configs[name] = config
            started: set[str] = set()
            try:
                for item in self._order:
                    if item in names:
                        await self._activate(item, publish=False)
                        started.add(item)
                with self._guard:
                    for item in started:
                        self._state[item] = "active"
            except BaseException:
                await self._drain(started)
                await self._dispose(started)
                raise

        await self._atomic(operation)
