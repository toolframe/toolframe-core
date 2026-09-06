"""Startup-configured, data-only access for Toolframe tools.

FastAPI owns the application lifecycle but does not provide a database access
API. The Toolframe runtime configures this adapter before it imports any MCP
tool, so tool code can reuse one supported contract without opening SQLite
connections or reading runtime environment variables itself.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pandas as pd
from toolframe_database import (
    DashboardDatabase,
    DashboardDatabaseError,
    ExecuteResult,
)

__all__ = [
    "Database",
    "DatabaseError",
    "ExecuteResult",
    "get_database",
]


DatabaseError = DashboardDatabaseError
_LOGGER = logging.getLogger("toolframe.database")
_DEFAULT_MAX_ROWS = 100
_DEFAULT_MAX_BYTES = 1024 * 1024
_PROTECTED_TABLE_PREFIXES = ("_toolframe_", "sqlite_")


class Database:
    """Bounded reads and data mutations for startup-loaded MCP tools.

    Obtain the process instance with :func:`get_database`. ``query`` accepts
    row-returning, read-only SQL; ``execute`` accepts DML only. Durable schema
    changes must be authored as the next migration and applied on restart.
    """

    __slots__ = ("_backend", "_read_only", "_ready")

    def __init__(self, backend, *, ready=True, read_only=False) -> None:
        self._backend = backend
        self._ready = ready
        self._read_only = read_only

    def __getattr__(self, name):
        return getattr(self._backend, name)

    def _activate(self):
        self._ready = True

    def _check_access(self, *, write=False):
        if not self._ready:
            raise DatabaseError(
                "database_not_ready",
                "The database schema is not ready yet.",
                recovery_hint="Keep imports side-effect free and access data inside an active plugin or tool.",
            )
        if write and self._read_only:
            raise DatabaseError(
                "preflight_write_forbidden",
                "Application writes are disabled during check.",
                recovery_hint="Move business mutations into tool calls or plugin setup.",
            )

    @property
    def path(self) -> Path:
        """Return the configured database path for diagnostics."""

        return self._backend.path

    def query(
        self,
        sql: str,
        params: Mapping[str, Any] | Sequence[Any] = (),
        *,
        max_rows: int | None = None,
        max_bytes: int | None = None,
    ) -> pd.DataFrame:
        """Run one bounded, read-only statement with bound parameters."""

        self._check_access()
        return self._backend.query(
            sql,
            params,
            max_rows=max_rows,
            max_bytes=max_bytes,
        )

    def execute(
        self,
        sql: str,
        params: Mapping[str, Any] | Sequence[Any] = (),
    ) -> ExecuteResult:
        """Atomically commit one parameterized INSERT, UPDATE, DELETE, or REPLACE."""

        self._check_access(write=True)
        return self._backend.execute(sql, params)

    def insert(self, table, values, *, id_column="id"):
        self._check_access(write=True)
        return self._backend.insert(table, values, id_column=id_column)

    def transaction(self):
        self._check_access(write=True)
        return self._backend.transaction()

    def run_once(self, namespace, key, input_value, mutation):
        self._check_access(write=True)
        return self._backend.run_once(namespace, key, input_value, mutation)

    def public_info(self):
        return self._backend.public_info()

    def close(self):
        self._ready = False
        self._backend.close()

    def connection(self, *, read_only=False):
        self._check_access(write=not read_only)
        return self._backend.connection(read_only=read_only)

    def begin_locked(self, connection, key):
        self._check_access(write=True)
        return self._backend.begin_locked(connection, key)


_configuration_lock = threading.RLock()
_configured_database: Database | None = None


def _configure(
    database_path: Path | None = None,
    *,
    provider=None,
    ready=True,
    read_only=False,
    logger: logging.Logger | None = None,
    max_rows: int = _DEFAULT_MAX_ROWS,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> Database:
    """Configure the process adapter before importing user tools."""

    global _configured_database
    configured = Database(
        provider
        if provider is not None
        else DashboardDatabase(
            database_path,
            logger=logger or _LOGGER,
            max_rows=max_rows,
            max_bytes=max_bytes,
            allow_schema_changes=False,
            protected_table_prefixes=_PROTECTED_TABLE_PREFIXES,
        ),
        ready=ready,
        read_only=read_only,
    )
    with _configuration_lock:
        _configured_database = configured
    return configured


def get_database() -> Database:
    """Return database access configured by the Toolframe runtime."""

    with _configuration_lock:
        if _configured_database is None:
            raise RuntimeError(
                "toolframe_db must be configured before importing tools; "
                "start the tool through Toolframe check, invoke, or serve"
            )
        return _configured_database
