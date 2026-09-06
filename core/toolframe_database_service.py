"""Stable database contracts, separate from concrete provider plugin identities."""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Any, Protocol, runtime_checkable

import pandas as pd
from fastapi_cordis import ServiceKey
from toolframe_database import ExecuteResult, SQLiteProvider
from toolframe_database_config import DatabaseConfigurationError, DatabaseSettings
from toolframe_idempotency import IdempotencyResult, IdempotencyTransaction

Parameters = Mapping[str, Any] | Sequence[Any]


@runtime_checkable
class DatabaseTransaction(Protocol):
    def query(
        self,
        sql: str,
        params: Parameters = (),
        *,
        max_rows: int | None = None,
        max_bytes: int | None = None,
    ) -> pd.DataFrame: ...

    def execute(self, sql: str, params: Parameters = ()) -> ExecuteResult: ...

    def insert(
        self, table: str, values: Mapping[str, Any], *, id_column: str = "id"
    ) -> int: ...


@runtime_checkable
class DatabaseService(DatabaseTransaction, Protocol):
    """The observable service consumed by tools, dashboards, and business plugins."""

    def transaction(self) -> AbstractContextManager[DatabaseTransaction]: ...

    def run_once(
        self,
        namespace: str,
        key: str,
        input_value: Any,
        mutation: Callable[[IdempotencyTransaction], Any],
    ) -> IdempotencyResult: ...

    def public_info(self) -> dict[str, Any]: ...

    def close(self) -> None: ...


@runtime_checkable
class DatabaseProvider(DatabaseService, Protocol):
    """Host integration operations; never exposed as tool or browser endpoints."""

    dialect: str
    identity: str
    settings: DatabaseSettings

    def connection(self, *, read_only: bool = False) -> AbstractContextManager[Any]: ...

    def begin_locked(self, connection: Any, key: str) -> None: ...


DATABASE = ServiceKey("toolframe.database", DatabaseService)


def create_provider(
    settings: DatabaseSettings, *, logger=None, audit_sink=None
) -> DatabaseProvider:
    """Build a lazy provider; the plugin lifecycle owns opening and closing it."""
    kwargs = {"logger": logger, "audit_sink": audit_sink}
    pending = None
    provider = None
    try:
        if settings.provider == "sqlite":
            provider = SQLiteProvider(
                settings=settings,
                allow_schema_changes=False,
                protected_table_prefixes=("_toolframe_", "sqlite_"),
                **kwargs,
            )
        elif settings.provider == "postgresql":
            from toolframe_database_postgresql import PostgreSQLProvider

            provider = PostgreSQLProvider(settings, **kwargs)
        else:
            module_name, factory_name = settings.provider.split(":")
            factory = getattr(importlib.import_module(module_name), factory_name)
            provider = factory(settings, **kwargs)
        if not isinstance(provider, DatabaseProvider) or provider.dialect not in {
            "sqlite",
            "postgresql",
        }:
            raise TypeError("unsupported provider contract")
        return provider
    except Exception:  # noqa: BLE001 - never expose factory errors containing secrets.
        if provider is not None and callable(getattr(provider, "close", None)):
            try:
                provider.close()
            except Exception as error:  # noqa: BLE001 - preserve the initialization error.
                (logger or logging.getLogger(__name__)).error(
                    "provider cleanup failed exception_type=%s", type(error).__name__
                )
        pending = DatabaseConfigurationError(
            "database provider could not be initialized"
        )
    # Third-party factory/driver exceptions can contain connection secrets.
    raise pending
