# /// script
# requires-python = ">=3.14,<3.15"
# dependencies = []
# ///

"""Narrow read capability for the example Entry Register domain."""

from typing import Any

from fastmcp.tools import tool
from toolframe_db import DatabaseError, get_database
from toolframe_runtime import ToolframeError

from domains.entry_register.model.entries import (
    EntryValidationError,
    list_entries,
    validate_limit,
)

_PEER_LIMIT = 100


def _plain_value(value: Any) -> Any:
    item = getattr(value, "item", None)
    if callable(item):
        return item()
    return value


@tool
def entry_register_list_entries(limit: int = 50) -> dict[str, Any]:
    """List up to 100 newest Entry Register entries for review.

    Use this bounded business query when a peer agent needs the current entry
    labels, values, versions, and timestamps. It does not create or edit data.
    """

    try:
        bounded_limit = validate_limit(limit, maximum=_PEER_LIMIT)
        frame = list_entries(get_database(), limit=bounded_limit)
    except EntryValidationError as error:
        raise ToolframeError(
            "entry_register_invalid_limit",
            str(error),
            retryable=False,
            details={"recoveryHint": f"Choose a limit from 1 to {_PEER_LIMIT}."},
        ) from error
    except DatabaseError as error:
        raise ToolframeError(
            error.code,
            error.message,
            retryable=error.retryable,
            details={**dict(error.details), "recoveryHint": error.recovery_hint},
        ) from error

    entries = [
        {str(key): _plain_value(value) for key, value in record.items()}
        for record in frame.to_dict(orient="records")
    ]
    return {
        "entries": entries,
        "count": len(entries),
        "limit": bounded_limit,
        "nextAction": "Use the human dashboard for entry changes.",
    }
