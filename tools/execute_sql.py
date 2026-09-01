# /// script
# requires-python = ">=3.14,<3.15"
# dependencies = [
#     "fastmcp==4.0.0",
# ]
# ///

"""Reference FastMCP tool for executing one SQLite statement."""

import logging as _logging
import math as _math
import os as _os
import sqlite3 as _sqlite3
import traceback as _traceback
import uuid as _uuid
from typing import Any as _Any

from fastmcp.tools import ToolResult as _ToolResult
from fastmcp.tools import tool as _tool


_LOGGER = _logging.getLogger(__name__)
_MAX_SQL_BYTES = 1_048_576
_SQL_RESULT_ROW_LIMIT = 100
_BUSY_TIMEOUT_SECONDS = 5.0


def _trace_id() -> str:
    return str(_uuid.uuid4())


def _result(content: _Any) -> _ToolResult:
    return _ToolResult(structured_content=content)


def _error_result(
    *,
    code: str,
    message: str,
    retryable: bool,
    trace_id: str | None = None,
    details: dict[str, _Any] | None = None,
) -> _ToolResult:
    trace_id = trace_id or _trace_id()
    error = {
        "code": code,
        "message": message,
        "retryable": retryable,
        "details": details or {},
        "traceId": trace_id,
    }
    text = f"SQL tool error ({code}): {message} [traceId={trace_id}]"
    return _ToolResult(
        content=text,
        structured_content={"error": error},
        meta={"retryable": retryable, "traceId": trace_id},
        is_error=True,
    )


def _log_exception(trace_id: str, error: Exception) -> None:
    """Log stack frames while redacting exception text that could contain SQL."""
    frames = "".join(_traceback.format_tb(error.__traceback__))
    if not frames:
        frames = "<traceback unavailable>\n"
    safe_traceback = (
        f"Traceback (most recent call last):\n{frames}"
        f"{type(error).__name__}: <details redacted>"
    )
    _LOGGER.error(
        "SQL execution failed traceId=%s exception_type=%s\n%s",
        trace_id,
        type(error).__name__,
        safe_traceback,
    )


def _error_details(error: Exception, recovery_hint: str) -> dict[str, str]:
    return {
        "exception_type": type(error).__name__,
        "recovery_hint": recovery_hint,
    }


def _is_busy_error(error_text: str) -> bool:
    return "busy" in error_text or "locked" in error_text


def _is_transient_io_error(error_text: str) -> bool:
    return any(
        marker in error_text
        for marker in (
            "disk i/o error",
            "i/o error",
            "database or disk is full",
            "unable to open database file",
        )
    )


def _is_multi_statement_error(error: _sqlite3.Error) -> bool:
    error_text = str(error).lower()
    return "one statement at a time" in error_text or "only execute one statement" in error_text


def _is_syntax_error(error_text: str) -> bool:
    return any(
        marker in error_text
        for marker in (
            "syntax error",
            "incomplete input",
            "unrecognized token",
            "no such module",
        )
    )


def _has_executable_sql(sql: str) -> bool:
    """Return false for input containing only separators and SQL comments."""
    index = 0
    while index < len(sql):
        if sql[index].isspace() or sql[index] == ";":
            index += 1
            continue
        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            index = len(sql) if newline < 0 else newline + 1
            continue
        if sql.startswith("/*", index):
            closing = sql.find("*/", index + 2)
            index = len(sql) if closing < 0 else closing + 2
            continue
        return True
    return False


def _sqlite_error_result(error: _sqlite3.Error) -> _ToolResult:
    trace_id = _trace_id()
    _log_exception(trace_id, error)
    error_text = str(error).lower()

    if _is_multi_statement_error(error):
        return _error_result(
            code="multi_statement",
            message="Only one SQL statement may be executed per call.",
            retryable=False,
            trace_id=trace_id,
            details=_error_details(error, "Submit exactly one SQLite statement."),
        )

    if isinstance(error, _sqlite3.OperationalError) and _is_busy_error(error_text):
        return _error_result(
            code="database_busy",
            message="The database is busy or locked; retry the operation.",
            retryable=True,
            trace_id=trace_id,
            details=_error_details(error, "Wait for the other database operation to finish, then retry."),
        )

    if isinstance(error, _sqlite3.OperationalError) and _is_transient_io_error(error_text):
        return _error_result(
            code="transient_io",
            message="A transient database I/O failure occurred; retry the operation.",
            retryable=True,
            trace_id=trace_id,
            details=_error_details(error, "Check temporary storage availability, then retry."),
        )

    if isinstance(error, _sqlite3.IntegrityError):
        return _error_result(
            code="constraint_violation",
            message="The SQL statement violated a database constraint.",
            retryable=False,
            trace_id=trace_id,
            details=_error_details(error, "Correct the data or constraint conflict, then retry."),
        )

    if _is_syntax_error(error_text):
        return _error_result(
            code="syntax_error",
            message="The SQL statement has invalid SQLite syntax.",
            retryable=False,
            trace_id=trace_id,
            details=_error_details(error, "Correct the SQL syntax and submit one statement."),
        )

    return _error_result(
        code="sql_error",
        message="The SQL statement could not be executed.",
        retryable=False,
        trace_id=trace_id,
        details=_error_details(error, "Inspect the statement and database schema, then retry if appropriate."),
    )


def _unexpected_error_result(error: Exception) -> _ToolResult:
    trace_id = _trace_id()
    _log_exception(trace_id, error)
    return _error_result(
        code="internal_error",
        message="The SQL tool encountered an unexpected server error.",
        retryable=False,
        trace_id=trace_id,
        details=_error_details(error, "Inspect the server logs using the traceId before retrying."),
    )


def _convert_value(value: _Any) -> _Any:
    if value is None:
        return None
    if isinstance(value, float) and not _math.isfinite(value):
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<blob: {len(value)} bytes>"
    return value


def _execute_one(database_path: str, sql: str) -> dict[str, _Any]:
    connection = _sqlite3.connect(database_path, timeout=_BUSY_TIMEOUT_SECONDS)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        cursor = connection.execute(sql)

        if cursor.description is not None:
            columns = [column[0] for column in cursor.description]
            rows = [
                [_convert_value(value) for value in row]
                for row in cursor.fetchmany(_SQL_RESULT_ROW_LIMIT + 1)
            ]
            truncated = len(rows) > _SQL_RESULT_ROW_LIMIT
            if connection.in_transaction:
                # SQLite must step a DML RETURNING statement to completion
                # before it can commit, even though the MCP response is capped.
                for _ in cursor:
                    pass
                connection.commit()
            return {
                "kind": "query",
                "columns": columns,
                "rows": rows[:_SQL_RESULT_ROW_LIMIT],
                "truncated": truncated,
            }

        affected_rows = cursor.rowcount
        if affected_rows < 0:
            affected_rows = 0
        connection.commit()
        return {
            "kind": "mutation",
            "affected_rows": int(affected_rows),
            "last_insert_rowid": cursor.lastrowid,
        }
    except Exception:
        try:
            connection.rollback()
        except Exception:
            pass
        raise
    finally:
        connection.close()


@_tool(
    description=(
        "Execute exactly one SQLite statement against the database selected by "
        "LITE_MCP_DB. This unauthenticated LAN server allows writes, DDL, and "
        "PRAGMA statements, which can permanently change the database."
    )
)
def execute_sql(sql: str) -> _ToolResult:
    """Execute one SQLite statement.

    This unauthenticated LAN server allows writes, DDL, and PRAGMA statements;
    those operations can permanently change the database.
    """
    try:
        normalized_sql = sql.strip()
        if not normalized_sql or not _has_executable_sql(normalized_sql):
            return _error_result(
                code="empty_sql",
                message="SQL must not be empty.",
                retryable=False,
                details={"recovery_hint": "Submit one non-empty SQLite statement."},
            )

        if len(normalized_sql.encode("utf-8")) > _MAX_SQL_BYTES:
            return _error_result(
                code="sql_too_long",
                message="SQL exceeds the 1,048,576-byte UTF-8 limit.",
                retryable=False,
                details={"recovery_hint": "Shorten the SQL statement and retry."},
            )

        database_path = _os.environ.get("LITE_MCP_DB", "").strip()
        if not database_path:
            return _error_result(
                code="missing_database_path",
                message="LITE_MCP_DB must contain the absolute database path.",
                retryable=False,
                details={"recovery_hint": "Set LITE_MCP_DB to an absolute SQLite database path."},
            )

        return _result(_execute_one(database_path, normalized_sql))
    except _sqlite3.Error as error:
        return _sqlite_error_result(error)
    except Exception as error:
        return _unexpected_error_result(error)
