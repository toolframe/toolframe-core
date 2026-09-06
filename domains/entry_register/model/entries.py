"""Entry validation and persistence shared by this domain's UI and tools."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

import pandas as pd

TABLE = "entry_register_entries"
MAX_LABEL_LENGTH = 80
MAX_LIST_LIMIT = 1_000


class EntryValidationError(ValueError):
    """The user supplied an entry value that can be corrected."""


class EntryConflictError(RuntimeError):
    """The entry changed after it was shown to this dashboard session."""


class EntryDatabase(Protocol):
    """Small database surface consumed by the domain model."""

    def query(
        self,
        sql: str,
        params: Mapping[str, Any] | Sequence[Any] = (),
        *,
        max_rows: int | None = None,
        max_bytes: int | None = None,
    ) -> pd.DataFrame: ...

    def execute(
        self, sql: str, params: Mapping[str, Any] | Sequence[Any] = ()
    ) -> Any: ...

    def insert(
        self, table: str, values: Mapping[str, Any], *, id_column: str = "id"
    ) -> int: ...


def validate_label(value: Any) -> str:
    """Return a normalized, non-empty entry label."""

    label = str(value).strip()
    if not label:
        raise EntryValidationError("Label is required.")
    if len(label) > MAX_LABEL_LENGTH:
        raise EntryValidationError(
            f"Label must be {MAX_LABEL_LENGTH} characters or fewer."
        )
    return label


def validate_value(value: Any) -> float:
    """Return a finite numeric entry value."""

    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise EntryValidationError("Value must be a number.") from error
    if not math.isfinite(number):
        raise EntryValidationError("Value must be finite.")
    return number


def validate_limit(value: Any, *, maximum: int = MAX_LIST_LIMIT) -> int:
    """Return a bounded positive list size."""

    if isinstance(value, bool):
        raise EntryValidationError(f"Limit must be between 1 and {maximum}.")
    try:
        limit = int(value)
    except (TypeError, ValueError) as error:
        raise EntryValidationError(f"Limit must be between 1 and {maximum}.") from error
    if limit < 1 or limit > maximum:
        raise EntryValidationError(f"Limit must be between 1 and {maximum}.")
    return limit


def list_entries(database: EntryDatabase, *, limit: int = 100) -> pd.DataFrame:
    """Return the newest entries as a bounded business view."""

    bounded_limit = validate_limit(limit)
    return database.query(
        f"""
        SELECT id, label, value, version, created_at, updated_at
        FROM {TABLE}
        ORDER BY id DESC
        LIMIT :limit
        """,
        {"limit": bounded_limit},
        max_rows=bounded_limit,
    )


def add_entry(database: EntryDatabase, *, label: Any, value: Any) -> int:
    """Create one validated entry and return its identifier."""

    return database.insert(
        TABLE,
        {"label": validate_label(label), "value": validate_value(value)},
    )


def update_entry(
    database: EntryDatabase,
    *,
    entry_id: int,
    expected_version: int,
    label: Any,
    value: Any,
) -> None:
    """Update one entry with an optimistic version guard."""

    result = database.execute(
        f"""
        UPDATE {TABLE}
        SET label = :label, value = :value, version = version + 1,
            updated_at = :updated_at
        WHERE id = :id AND version = :version
        """,
        {
            "label": validate_label(label),
            "value": validate_value(value),
            "id": int(entry_id),
            "version": int(expected_version),
            "updated_at": datetime.now(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
        },
    )
    if result.affected_rows != 1:
        raise EntryConflictError(
            f"Entry {entry_id} changed in another session or no longer exists."
        )
