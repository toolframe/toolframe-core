"""Asynchronous management journal interface, independent of plugin storage."""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from toolframe_sdk import PluginError

from ._state_sqlite import OperationQuery, SQLiteCoreState  # noqa: F401
from .redaction import SENSITIVE_KEY, secret_values


@dataclass(frozen=True, repr=False)
class StateSettings:
    backend: str = "sqlite"
    database_url: str = ""
    application_id: str = ""
    node_id: str = ""

    @classmethod
    def from_env(cls):
        backend = os.environ.get("TOOLFRAME_CORE_STATE_BACKEND", "sqlite")
        if backend not in {"sqlite", "postgresql"}:
            raise PluginError("core_state_config_invalid", "Unknown Core state backend")
        if backend == "sqlite":
            return cls()
        values = {
            name: os.environ.get(variable, "")
            for name, variable in (
                ("database_url", "TOOLFRAME_CORE_DATABASE_URL"),
                ("application_id", "TOOLFRAME_CORE_APPLICATION_ID"),
                ("node_id", "TOOLFRAME_CORE_NODE_ID"),
            )
        }
        if any(not value or len(value) > 4096 for value in values.values()):
            raise PluginError(
                "core_state_config_invalid",
                "Set Core database URL, application ID and stable node ID in the runtime environment",
            )
        return cls(backend=backend, **values)


class CoreState:
    """One serialized, durable call per await; cached secrets allow offline redaction.

    SQLite's connection belongs to one dedicated executor thread. PostgreSQL uses
    an async connection. Cancellation drains a submitted SQLite call before its
    caller exits; a cancelled PostgreSQL call poisons that host's journal session.
    """

    def __init__(self, settings):
        self.settings = settings
        self.backend = None
        self.executor = None
        self.lock = asyncio.Lock()
        self.secrets = set()
        self.failed = False
        self.closed = False

    @classmethod
    async def open(cls, home, *, settings=None):
        state = cls(settings or StateSettings.from_env())
        try:
            if state.settings.backend == "sqlite":
                state.executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="core-state"
                )

                def initialize():
                    state.backend = SQLiteCoreState(Path(home))

                await state._thread(initialize)
            else:
                if (Path(home) / "data/core.sqlite").exists():
                    raise PluginError(
                        "core_state_migration_required",
                        "Use a fresh application home or explicitly migrate the existing SQLite journal",
                    )
                from ._state_postgres import PostgresCoreState

                state.backend = await PostgresCoreState.open(state.settings)
                state.secrets.add(state.settings.database_url)
            for plugin in await state.plugins():
                for name in (
                    "last_candidate",
                    "current_candidate",
                    "previous_candidate",
                ):
                    if plugin[name]:
                        await state.candidate(plugin[name])
            await state.operations()
            return state
        except BaseException:
            await state.close()
            raise

    async def _thread(self, function, *args, **kwargs):
        future = asyncio.get_running_loop().run_in_executor(
            self.executor, partial(function, *args, **kwargs)
        )
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            await asyncio.shield(future)
            raise

    def _remember(self, value):
        if isinstance(value, dict):
            if isinstance(value.get("definition"), dict):
                self.secrets.update(
                    secret_values(
                        value.get("config", {}),
                        value["definition"].get("config_schema", {}),
                    )
                )
                # Schema descriptions and constraints are metadata, not secret
                # values. For example password.minLength=1 must not hide page 1.
                value = {
                    key: item for key, item in value.items() if key != "definition"
                }
            for key, item in value.items():
                if SENSITIVE_KEY.search(key):
                    self.secrets.update(secret_values(item, {"writeOnly": True}))
                else:
                    self._remember(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                self._remember(item)

    async def _call(self, method, *args, **kwargs):
        async with self.lock:
            if self.closed or self.failed:
                raise PluginError(
                    "core_state_unavailable",
                    "Core journal is unavailable; restart after recovery",
                )
            self._remember(args)
            self._remember(kwargs)
            try:
                if self.executor:
                    result = await self._thread(
                        getattr(self.backend, method), *args, **kwargs
                    )
                else:
                    result = await self.backend.call(method, *args, **kwargs)
            except BaseException:
                if not self.executor and self.backend.failed:
                    self.failed = True
                raise
            self._remember(result)
            return result

    async def close(self, *, clean=True):
        async with self.lock:
            if self.closed:
                return
            self.closed = True
            try:
                if self.backend:
                    if self.executor:
                        await self._thread(self.backend.close)
                    else:
                        await self.backend.close(clean=clean and not self.failed)
            finally:
                if self.executor:
                    self.executor.shutdown(wait=False)

    async def ping(self):
        if self.executor:
            return not self.closed
        return await self._call("ping")

    async def meta(self, key, default=None):
        return await self._call("meta", key, default)

    async def set_meta(self, key, value):
        return await self._call("set_meta", key, value)

    async def save_candidate(self, candidate):
        return await self._call("save_candidate", candidate)

    async def candidate(self, identifier):
        return await self._call("candidate", identifier)

    async def plugin(self, identifier):
        return await self._call("plugin", identifier)

    async def plugins(self):
        return await self._call("plugins")

    async def runtime(self, identifier, value):
        return await self._call("runtime", identifier, value)

    async def select(self, identifier, candidate_id):
        return await self._call("select", identifier, candidate_id)

    async def stopped(self, identifier, stopped):
        return await self._call("stopped", identifier, stopped)

    async def operation(self, identifier):
        return await self._call("operation", identifier)

    async def idempotent_operation(self, kind, target, request, idempotency=None):
        return await self._call(
            "idempotent_operation", kind, target, request, idempotency
        )

    async def new_operation(self, kind, target, request, idempotency=None):
        return await self._call("new_operation", kind, target, request, idempotency)

    async def update_operation(self, identifier, **values):
        return await self._call("update_operation", identifier, **values)

    async def operations(self, limit=50):
        return await self._call("operations", limit)

    async def operation_page(self, params=None):
        return await self._call("operation_page", params)

    async def reconcile_startup(self, host_run):
        return await self._call("reconcile_startup", host_run)
