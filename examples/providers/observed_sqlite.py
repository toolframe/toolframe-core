"""Example SQLite provider factory for an application-owned provider module."""

from __future__ import annotations

from typing import Any

from toolframe_database import SQLiteProvider
from toolframe_database_config import DatabaseSettings


class ObservedSQLiteProvider(SQLiteProvider):
    """SQLite provider with an example-specific public implementation marker."""

    def public_info(self) -> dict[str, Any]:
        info = super().public_info()
        info["implementation"] = "example.observed_sqlite"
        return info


def create(
    settings: DatabaseSettings, *, logger=None, audit_sink=None
) -> ObservedSQLiteProvider:
    """Create the example provider through Toolframe's provider factory contract."""

    return ObservedSQLiteProvider(
        settings=settings,
        logger=logger,
        audit_sink=audit_sink,
        allow_schema_changes=False,
        protected_table_prefixes=("_toolframe_", "sqlite_"),
    )
