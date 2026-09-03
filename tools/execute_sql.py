# /// script
# requires-python = ">=3.14,<3.15"
# dependencies = [
#     "fastmcp==4.0.0",
# ]
# ///

"""Fallback data-access example built on Lite MCP's shared database adapter."""

import logging as _logging
import math as _math
import uuid as _uuid
from typing import Any as _Any
from typing import Literal as _Literal

from fastmcp.tools import ToolResult as _ToolResult
from fastmcp.tools import tool as _tool
from lite_mcp_db import DatabaseError as _DatabaseError
from lite_mcp_db import get_database as _get_database

_LOGGER = _logging.getLogger(__name__)
_SqlValue = str | int | float | bool | None
_SqlParams = list[_SqlValue] | dict[str, _SqlValue] | None
_SqlMode = _Literal["read", "write"]


def _result(content: dict[str, _Any]) -> _ToolResult:
    return _ToolResult(structured_content=content)


def _error_result(
    *,
    code: str,
    message: str,
    retryable: bool,
    trace_id: str,
    details: dict[str, _Any],
) -> _ToolResult:
    error = {
        "code": code,
        "message": message,
        "retryable": retryable,
        "details": details,
        "traceId": trace_id,
    }
    return _ToolResult(
        content=f"SQL data tool error ({code}): {message} [traceId={trace_id}]",
        structured_content={"error": error},
        meta={"retryable": retryable, "traceId": trace_id},
        is_error=True,
    )


def _database_error_result(error: _DatabaseError) -> _ToolResult:
    return _error_result(
        code=error.code,
        message=error.message,
        retryable=error.retryable,
        trace_id=error.trace_id,
        details={
            **dict(error.details),
            "recoveryHint": error.recovery_hint,
        },
    )


def _unexpected_error_result(error: Exception) -> _ToolResult:
    trace_id = _uuid.uuid4().hex
    _LOGGER.exception(
        "SQL data tool failed traceId=%s exception_type=%s",
        trace_id,
        type(error).__name__,
    )
    return _error_result(
        code="internal_error",
        message="The SQL data tool encountered an unexpected server error.",
        retryable=False,
        trace_id=trace_id,
        details={
            "exception_type": type(error).__name__,
            "recoveryHint": "Inspect the server logs using the trace ID.",
        },
    )


def _convert_value(value: _Any) -> _Any:
    item = getattr(value, "item", None)
    if callable(item):
        try:
            value = item()
        except (TypeError, ValueError):
            pass
    if value is None:
        return None
    if isinstance(value, float) and not _math.isfinite(value):
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<blob: {len(value)} bytes>"
    return value


def _frame_payload(frame: _Any) -> dict[str, _Any]:
    return {
        "columns": [str(column) for column in frame.columns],
        "rows": [
            [_convert_value(value) for value in row]
            for row in frame.itertuples(index=False, name=None)
        ],
    }


@_tool(
    description=(
        "Fallback access to app data through Lite MCP's bounded database adapter. "
        "Use read mode for one parameterized row-returning statement or write mode "
        "for one parameterized INSERT, UPDATE, DELETE, or REPLACE. This tool cannot "
        "change schema, run PRAGMA/ATTACH/maintenance SQL, or mutate runtime-owned "
        "tables. Prefer the app's Business RPC commands and queries whenever they "
        "exist."
    )
)
def execute_sql(
    sql: str,
    params: _SqlParams = None,
    mode: _SqlMode = "read",
) -> _ToolResult:
    """Read or mutate app data through the configured runtime adapter.

    This is a fallback and implementation reference, not a schema-management
    tool. A primary coding agent must put durable schema changes in the next
    migration and restart Lite MCP.
    """

    if mode not in {"read", "write"}:
        return _error_result(
            code="invalid_mode",
            message="mode must be either 'read' or 'write'.",
            retryable=False,
            trace_id=_uuid.uuid4().hex,
            details={
                "recoveryHint": "Use read for queries or write for data mutations."
            },
        )

    try:
        database = _get_database()
        bindings = () if params is None else params
        if mode == "read":
            return _result(
                {
                    "kind": "query",
                    **_frame_payload(database.query(sql, bindings)),
                }
            )

        outcome = database.execute(sql, bindings)
        payload = {
            "kind": "mutation",
            "affected_rows": outcome.affected_rows,
            "last_insert_rowid": outcome.last_insert_rowid,
        }
        if outcome.rows is not None:
            payload.update(_frame_payload(outcome.rows))
        return _result(payload)
    except _DatabaseError as error:
        return _database_error_result(error)
    except Exception as error:  # noqa: BLE001 - preserve the MCP error boundary.
        return _unexpected_error_result(error)
