"""Startup-configured, data-only SQLite access for Toolframe tools.

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
from toolframe_dashboard import (
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

    __slots__ = ("_backend",)

    def __init__(self, backend: DashboardDatabase) -> None:
        self._backend = backend

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

        return self._backend.execute(sql, params)


_configuration_lock = threading.RLock()
_configured_database: Database | None = None


def _configure(
    database_path: Path,
    *,
    logger: logging.Logger | None = None,
    max_rows: int = _DEFAULT_MAX_ROWS,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> Database:
    """Configure the process adapter before importing user tools."""

    global _configured_database
    configured = Database(
        DashboardDatabase(
            database_path,
            logger=logger or _LOGGER,
            max_rows=max_rows,
            max_bytes=max_bytes,
            allow_schema_changes=False,
            protected_table_prefixes=_PROTECTED_TABLE_PREFIXES,
        )
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
