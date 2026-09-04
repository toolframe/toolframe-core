"""Stable, agent-facing helpers for toolframe Streamlit dashboards.

Dashboard modules are trusted local Python code.  The database API in this
module is a guardrail that makes the safe path easy; it is not a sandbox.
"""

from __future__ import annotations

import contextvars
import hashlib
import importlib
import importlib.util
import inspect
import keyword
import logging
import math
import os
import re
import sqlite3
import stat
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType, ModuleType, TracebackType
from typing import Any, Self
from urllib.parse import quote

import pandas as pd
from toolframe_architecture import validate_source_architecture
from toolframe_artifacts import ArtifactStore
from toolframe_idempotency import (
    IdempotencyError,
    IdempotencyResult,
    IdempotencyStore,
    IdempotencyTransaction,
)
from toolframe_idempotency import _audit_context as _idempotency_audit_context

__all__ = [
    "DashboardContext",
    "DashboardDatabase",
    "DashboardDatabaseError",
    "DashboardInteractionError",
    "DashboardWidget",
    "ExecuteResult",
    "IdempotencyError",
    "IdempotencyResult",
    "IdempotencyTransaction",
    "discover_widgets",
    "get_context",
    "preflight_app",
    "render_error_card",
    "render_widget",
    "render_widgets",
]


_LOGGER = logging.getLogger("toolframe.dashboard")
_DEFAULT_MAX_ROWS = 10_000
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024
_MAX_SQL_BYTES = 1024 * 1024
_BUSY_TIMEOUT_MS = 5_000
_DEFAULT_WIDGET_ORDER = 1_000
_DASHBOARD_SESSION_STATE_KEY = "_toolframe_dashboard_session_id"
_DOMAIN_NAME = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")
_WIDGET_NAME = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")
_DOMAIN_DIRECTORIES = ("llms", "model", "tools", "ui", "migrations")


def _sqlite_actions(*names: str) -> frozenset[int]:
    return frozenset(
        value
        for name in names
        if isinstance((value := getattr(sqlite3, name, None)), int)
    )


_READ_ACTIONS = _sqlite_actions("SQLITE_READ", "SQLITE_SELECT", "SQLITE_RECURSIVE")
_DML_ACTIONS = _sqlite_actions("SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_DELETE")
_DDL_ACTIONS = _sqlite_actions(
    "SQLITE_CREATE_INDEX",
    "SQLITE_CREATE_TABLE",
    "SQLITE_CREATE_TRIGGER",
    "SQLITE_CREATE_VIEW",
    "SQLITE_DROP_INDEX",
    "SQLITE_DROP_TABLE",
    "SQLITE_DROP_TRIGGER",
    "SQLITE_DROP_VIEW",
    "SQLITE_ALTER_TABLE",
)
_ALWAYS_FORBIDDEN_ACTIONS = _sqlite_actions(
    "SQLITE_ATTACH",
    "SQLITE_DETACH",
    "SQLITE_PRAGMA",
    "SQLITE_TRANSACTION",
    "SQLITE_SAVEPOINT",
    "SQLITE_ANALYZE",
    "SQLITE_CREATE_VTABLE",
    "SQLITE_DROP_VTABLE",
    "SQLITE_CREATE_TEMP_INDEX",
    "SQLITE_CREATE_TEMP_TABLE",
    "SQLITE_CREATE_TEMP_TRIGGER",
    "SQLITE_CREATE_TEMP_VIEW",
    "SQLITE_DROP_TEMP_INDEX",
    "SQLITE_DROP_TEMP_TABLE",
    "SQLITE_DROP_TEMP_TRIGGER",
    "SQLITE_DROP_TEMP_VIEW",
)
_FUNCTION_ACTION = getattr(sqlite3, "SQLITE_FUNCTION", -1)
_REINDEX_ACTION = getattr(sqlite3, "SQLITE_REINDEX", -1)
_SQLITE_SCHEMA_TABLES = frozenset(
    {"sqlite_master", "sqlite_schema", "sqlite_temp_master"}
)
_POLICY_REJECTION_CODES = frozenset(
    {
        "empty_sql",
        "forbidden_sql",
        "invalid_parameters",
        "invalid_sql",
        "multi_statement",
        "protected_table_forbidden",
        "read_only_required",
        "schema_change_forbidden",
        "sql_too_long",
        "write_statement_required",
    }
)


class DashboardDatabaseError(RuntimeError):
    """An agent/browser-safe, structured database-helper failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        trace_id: str | None = None,
        recovery_hint: str = "Inspect the server log using the trace ID.",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        self.trace_id = trace_id or uuid.uuid4().hex
        self.recovery_hint = recovery_hint
        self.details = MappingProxyType(dict(details or {}))
        super().__init__(f"{message} [traceId={self.trace_id}]")


class DashboardInteractionError(RuntimeError):
    """An expected, user-correctable business interaction failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        trace_id: str | None = None,
        recovery_hint: str,
    ) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        self.trace_id = trace_id or uuid.uuid4().hex
        self.recovery_hint = recovery_hint
        super().__init__(f"{message} [traceId={self.trace_id}]")


def render_error_card(
    error: DashboardInteractionError | DashboardDatabaseError | IdempotencyError,
    *,
    title: str = "This card could not complete the action",
) -> None:
    """Render the one standard card for expected interaction failures.

    Programming defects are deliberately rejected so they can propagate to
    Toolframe's fatal dashboard reporter instead of being disguised as a
    recoverable business error.
    """

    expected_database_codes = {
        "constraint_violation",
        "database_busy",
        "invalid_parameters",
    }
    expected_idempotency_codes = {
        "idempotency_invalid_input",
        "idempotency_invalid_key",
        "idempotency_key_mismatch",
    }
    if isinstance(error, DashboardDatabaseError):
        expected = error.retryable or error.code in expected_database_codes
    elif isinstance(error, IdempotencyError):
        expected = error.retryable or error.code in expected_idempotency_codes
    elif isinstance(error, DashboardInteractionError):
        expected = True
    else:  # pragma: no cover - the annotation documents the public contract.
        expected = False
    if not expected:
        raise error

    import streamlit as st

    retry_text = "yes" if error.retryable else "no"
    copyable = "\n".join(
        (
            "Please ask the master development agent to inspect this Toolframe app error.",
            f"Code: {error.code}",
            f"Trace ID: {error.trace_id}",
            f"Retryable: {retry_text}",
            f"Message: {error.message}",
            f"Recovery: {error.recovery_hint}",
        )
    )
    st.error(
        f"**{title}**\n\n{error.message}\n\n"
        "Copy this if the suggested correction does not resolve it:\n\n"
        f"```text\n{copyable}\n```"
    )


@dataclass(frozen=True, slots=True)
class ExecuteResult:
    """Result metadata for a committed DML or DDL statement."""

    affected_rows: int
    last_insert_rowid: int | None
    rows: pd.DataFrame | None = None


@dataclass(frozen=True, slots=True)
class DashboardContext:
    """Stable capabilities supplied to one dashboard widget."""

    db: DashboardDatabase
    workspace: Path
    logger: logging.Logger
    widget_id: str
    runtime: Mapping[str, Any]
    artifacts: ArtifactStore | None = None


@dataclass(frozen=True, slots=True)
class DashboardWidget:
    """A validated widget module discovered from a domain ``ui`` directory."""

    widget_id: str
    title: str
    order: int
    path: Path
    module_name: str
    module: ModuleType = field(repr=False, compare=False)
    domain: str


@dataclass(frozen=True, slots=True)
class _SdkConfiguration:
    app_dir: Path
    domains_dir: Path
    base_context: DashboardContext


@dataclass(slots=True)
class _PolicyState:
    mode: str
    allow_schema_changes: bool = True
    protected_table_prefixes: tuple[str, ...] = ()
    statement_keyword: str = ""
    saw_write: bool = False
    saw_user_insert: bool = False
    saw_trigger_write: bool = False
    denied_code: str | None = None
    denied_message: str | None = None
    denied_hint: str | None = None

    def deny(self, code: str, message: str, hint: str) -> int:
        if self.denied_code is None:
            self.denied_code = code
            self.denied_message = message
            self.denied_hint = hint
        return sqlite3.SQLITE_DENY

    def authorize(
        self,
        action: int,
        argument_one: str | None,
        argument_two: str | None,
        _database_name: str | None,
        trigger_name: str | None,
    ) -> int:
        if action in _ALWAYS_FORBIDDEN_ACTIONS:
            code = "read_only_required" if self.mode == "query" else "forbidden_sql"
            return self.deny(
                code,
                "The SQL statement is not allowed by the database policy.",
                "Use query() for reads and execute() only for regular data mutations.",
            )

        if action == _FUNCTION_ACTION:
            function_name = (argument_two or argument_one or "").casefold()
            if function_name in {"load_extension", "readfile", "writefile"}:
                return self.deny(
                    "forbidden_sql",
                    "SQLite extension and filesystem functions are not allowed.",
                    "Remove the forbidden function from the statement.",
                )
            return sqlite3.SQLITE_OK

        if action == _REINDEX_ACTION:
            if self.mode == "execute" and self.statement_keyword == "CREATE":
                # SQLite emits SQLITE_REINDEX while populating a new persistent
                # index. A top-level REINDEX is rejected before authorization.
                return sqlite3.SQLITE_OK
            return self.deny(
                "forbidden_sql",
                "Explicit SQLite maintenance statements are not allowed.",
                (
                    "Use regular DML or DDL instead of REINDEX."
                    if self.allow_schema_changes
                    else "Use regular app-data DML instead of REINDEX."
                ),
            )

        if action in _DDL_ACTIONS:
            if self.mode == "query":
                return self.deny(
                    "read_only_required",
                    "query() accepts read-only SQL only.",
                    "Move the mutation to the supported write helper.",
                )
            if not self.allow_schema_changes:
                return self.deny(
                    "schema_change_forbidden",
                    "Schema changes are not allowed through the runtime database adapter.",
                    "Add the next contiguous migration and restart Toolframe.",
                )
            self.saw_write = True
            if trigger_name is not None:
                self.saw_trigger_write = True
            return sqlite3.SQLITE_OK

        if action in _DML_ACTIONS:
            if self.mode == "query":
                return self.deny(
                    "read_only_required",
                    "query() accepts read-only SQL only.",
                    "Move the mutation to the supported write helper.",
                )
            table_name = (argument_one or "").casefold()
            if any(
                table_name.startswith(prefix)
                for prefix in self.protected_table_prefixes
            ):
                return self.deny(
                    "protected_table_forbidden",
                    "Runtime-owned SQLite tables cannot be changed through this adapter.",
                    "Write only app-owned data tables; use migrations for schema changes.",
                )
            self.saw_write = True
            if trigger_name is not None:
                self.saw_trigger_write = True
            if (
                action == getattr(sqlite3, "SQLITE_INSERT", -1)
                and (argument_one or "").casefold() not in _SQLITE_SCHEMA_TABLES
                and trigger_name is None
            ):
                self.saw_user_insert = True
            return sqlite3.SQLITE_OK

        if action in _READ_ACTIONS:
            return sqlite3.SQLITE_OK

        return self.deny(
            "forbidden_sql",
            "The SQL statement uses an unsupported SQLite operation.",
            (
                "Use ordinary SELECT, DML, or non-virtual persistent DDL."
                if self.allow_schema_changes
                else "Use an ordinary row-returning query or app-data DML."
            ),
        )


class _SdkFailure(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        recovery_hint: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        self.recovery_hint = recovery_hint
        self.details = dict(details or {})
        super().__init__(message)


class DashboardDatabase:
    """Bounded SQLite access for trusted dashboard code.

    A fresh SQLite connection is created for each standalone operation.  A
    transaction owns one connection for the lifetime of its ``with`` block.
    Connections are never shared between Streamlit session threads.
    """

    def __init__(
        self,
        path: Path,
        *,
        logger: logging.Logger | None = None,
        max_rows: int = _DEFAULT_MAX_ROWS,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        audit_sink: Any | None = None,
        allow_schema_changes: bool = True,
        protected_table_prefixes: Sequence[str] = (),
    ) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        self.logger = logger or _LOGGER
        self.max_rows = _validate_hard_limit("max_rows", max_rows)
        self.max_bytes = _validate_hard_limit("max_bytes", max_bytes)
        self.audit_sink = audit_sink
        self.allow_schema_changes = bool(allow_schema_changes)
        self.protected_table_prefixes = tuple(
            _validate_table_prefix(prefix) for prefix in protected_table_prefixes
        )
        self._idempotency = IdempotencyStore(
            self.path, audit_sink=audit_sink, logger=self.logger
        )

    def query(
        self,
        sql: str,
        params: Mapping[str, Any] | Sequence[Any] = (),
        *,
        max_rows: int | None = None,
        max_bytes: int | None = None,
    ) -> pd.DataFrame:
        """Execute one read-only statement and return a bounded DataFrame."""

        started_at = time.monotonic()
        operation_id = uuid.uuid4().hex
        try:
            connection = self._connect(read_only=True)
        except Exception as error:  # noqa: BLE001 - normalize the public boundary.
            self._raise_public(error, sql)

        try:
            return self._query_on_connection(
                connection,
                sql,
                params,
                max_rows=max_rows,
                max_bytes=max_bytes,
            )
        except DashboardDatabaseError as error:
            if error.code == "read_only_required":
                self._emit_audit(
                    "dashboard_mutation_rejected",
                    level="WARNING",
                    payload=_statement_payload(sql, params),
                    operationId=operation_id,
                    method="query",
                    outcome="rejected",
                    durationMs=_duration_ms(started_at),
                    retryable=error.retryable,
                    error=_error_payload(error),
                    errorTraceId=error.trace_id,
                )
            raise
        finally:
            connection.close()

    def execute(
        self,
        sql: str,
        params: Mapping[str, Any] | Sequence[Any] = (),
    ) -> ExecuteResult:
        """Execute and atomically commit one permitted DML or DDL statement."""

        operation_id = uuid.uuid4().hex
        started_at = time.monotonic()
        self._emit_audit(
            "dashboard_mutation_started",
            payload=_statement_payload(sql, params),
            operationId=operation_id,
            method="execute",
        )
        try:
            connection = self._connect(read_only=False)
        except Exception as error:  # noqa: BLE001 - normalize the public boundary.
            try:
                self._raise_public(error, sql)
            except DashboardDatabaseError as public_error:
                self._emit_statement_failure(
                    operation_id, public_error, started_at=started_at
                )
                raise

        try:
            connection.execute("BEGIN")
            result, policy = self._execute_on_connection(connection, sql, params)
            connection.commit()
        except DashboardDatabaseError as error:
            _rollback_quietly(connection, self.logger)
            self._emit_statement_failure(operation_id, error, started_at=started_at)
            raise
        except Exception as error:  # noqa: BLE001 - normalize the public boundary.
            _rollback_quietly(connection, self.logger)
            try:
                self._raise_public(error, sql)
            except DashboardDatabaseError as public_error:
                self._emit_statement_failure(
                    operation_id, public_error, started_at=started_at
                )
                raise
        finally:
            connection.close()
        self._emit_audit(
            "dashboard_mutation_completed",
            payload=_execute_result_payload(result, policy),
            operationId=operation_id,
            method="execute",
            outcome="success",
            durationMs=_duration_ms(started_at),
        )
        return result

    def transaction(self) -> _DashboardTransaction:
        """Return an atomic, non-nestable transaction context manager."""

        return _DashboardTransaction(self)

    def run_once(
        self,
        namespace: str,
        key: str,
        input_value: Any,
        mutation: Callable[[IdempotencyTransaction], Any],
    ) -> IdempotencyResult:
        """Run one explicitly keyed business mutation and replay its result."""

        if id(self) in _active_transactions.get():
            raise IdempotencyError(
                "idempotency_nested",
                "An idempotent mutation cannot start inside a dashboard transaction.",
                recovery_hint=(
                    "Make run_once own the complete transaction and use its callback transaction."
                ),
            )
        metadata = {
            "dashboardSessionId": None,
            "requestId": None,
            "sourceIp": None,
            "userAgent": None,
            "widget": None,
            **_dashboard_request_metadata.get(),
        }
        with _idempotency_audit_context(**metadata):
            return self._idempotency.run_once(namespace, key, input_value, mutation)

    def _emit_audit(
        self,
        event: str,
        *,
        level: str = "INFO",
        payload: Any = None,
        **fields: Any,
    ) -> dict[str, Any] | None:
        if self.audit_sink is None:
            return None
        metadata = {
            "dashboardSessionId": None,
            "requestId": None,
            "sourceIp": None,
            "userAgent": None,
            "widget": None,
            **_dashboard_request_metadata.get(),
        }
        metadata.update(fields)
        return self.audit_sink.emit(
            "audit", event, level=level, payload=payload, **metadata
        )

    def _emit_statement_failure(
        self,
        operation_id: str,
        error: DashboardDatabaseError,
        *,
        started_at: float,
        transaction_id: str | None = None,
        event: str = "dashboard_mutation_completed",
        **fields: Any,
    ) -> None:
        outcome = "rejected" if error.code in _POLICY_REJECTION_CODES else "failed"
        fields.setdefault("method", "execute")
        self._emit_audit(
            event,
            level="WARNING" if outcome == "rejected" else "ERROR",
            operationId=operation_id,
            transactionId=transaction_id,
            outcome=outcome,
            durationMs=_duration_ms(started_at),
            retryable=error.retryable,
            error=_error_payload(error),
            errorTraceId=error.trace_id,
            **fields,
        )

    def _connect(self, *, read_only: bool) -> sqlite3.Connection:
        if read_only:
            encoded_path = quote(os.fspath(self.path), safe="/")
            connection = sqlite3.connect(
                f"file:{encoded_path}?mode=ro",
                uri=True,
                timeout=_BUSY_TIMEOUT_MS / 1000,
                isolation_level=None,
            )
        else:
            connection = sqlite3.connect(
                self.path,
                timeout=_BUSY_TIMEOUT_MS / 1000,
                isolation_level=None,
            )

        try:
            connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.enable_load_extension(False)
            if hasattr(connection, "setconfig") and hasattr(
                sqlite3, "SQLITE_DBCONFIG_DEFENSIVE"
            ):
                connection.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
            if hasattr(connection, "setlimit"):
                connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, _MAX_SQL_BYTES)
                connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, self.max_bytes)
            if read_only:
                connection.execute("PRAGMA query_only = ON")
            else:
                connection.execute("PRAGMA journal_mode = WAL")
            return connection
        except Exception:
            connection.close()
            raise

    def _query_on_connection(
        self,
        connection: sqlite3.Connection,
        sql: str,
        params: Mapping[str, Any] | Sequence[Any],
        *,
        max_rows: int | None,
        max_bytes: int | None,
    ) -> pd.DataFrame:
        policy = _PolicyState(
            "query",
            allow_schema_changes=self.allow_schema_changes,
            protected_table_prefixes=self.protected_table_prefixes,
        )
        try:
            normalized_sql = _validate_sql(sql)
            normalized_params = _validate_params(params, max_bytes=self.max_bytes)
            row_limit = _validate_call_limit("max_rows", max_rows, self.max_rows)
            byte_limit = _validate_call_limit("max_bytes", max_bytes, self.max_bytes)
            connection.set_authorizer(policy.authorize)
            cursor = connection.execute(normalized_sql, normalized_params)
            try:
                if cursor.description is None:
                    raise _SdkFailure(
                        "read_only_required",
                        "query() requires a statement that returns rows.",
                        recovery_hint="Use SELECT, VALUES, or a read-only WITH query.",
                    )
                return _dataframe_from_cursor(cursor, row_limit, byte_limit)
            finally:
                cursor.close()
        except DashboardDatabaseError:
            raise
        except Exception as error:  # noqa: BLE001 - normalize the public boundary.
            self._raise_public(error, sql, policy=policy)
        finally:
            connection.set_authorizer(None)

    def _execute_on_connection(
        self,
        connection: sqlite3.Connection,
        sql: str,
        params: Mapping[str, Any] | Sequence[Any],
    ) -> tuple[ExecuteResult, _PolicyState]:
        policy = _PolicyState(
            "execute",
            allow_schema_changes=self.allow_schema_changes,
            protected_table_prefixes=self.protected_table_prefixes,
        )
        try:
            normalized_sql = _validate_sql(sql)
            normalized_params = _validate_params(params, max_bytes=self.max_bytes)
            policy.statement_keyword = _leading_keyword(normalized_sql)
            if not policy.allow_schema_changes and policy.statement_keyword in {
                "ALTER",
                "CREATE",
                "DROP",
            }:
                raise _SdkFailure(
                    "schema_change_forbidden",
                    "Schema changes are not allowed through the runtime database adapter.",
                    recovery_hint=(
                        "Add the next contiguous migration and restart Toolframe."
                    ),
                )
            if policy.statement_keyword == "EXPLAIN":
                raise _SdkFailure(
                    "write_statement_required",
                    (
                        "execute() accepts DML or DDL statements only."
                        if policy.allow_schema_changes
                        else "execute() accepts app-data DML statements only."
                    ),
                    recovery_hint="Use query() for read-only query plans.",
                )
            if policy.statement_keyword in {"ANALYZE", "REINDEX"}:
                raise _SdkFailure(
                    "forbidden_sql",
                    "Explicit SQLite maintenance statements are not allowed.",
                    recovery_hint=(
                        "Use regular DML or persistent non-virtual DDL."
                        if policy.allow_schema_changes
                        else "Use regular app-data DML."
                    ),
                )
            connection.set_authorizer(policy.authorize)
            before_changes = connection.total_changes
            cursor = connection.execute(normalized_sql, normalized_params)
            try:
                if not policy.saw_write:
                    raise _SdkFailure(
                        "write_statement_required",
                        (
                            "execute() accepts DML or DDL statements only."
                            if policy.allow_schema_changes
                            else "execute() accepts app-data DML statements only."
                        ),
                        recovery_hint="Use query() for read-only SQL.",
                    )
                rows = (
                    _dataframe_from_cursor(cursor, self.max_rows, self.max_bytes)
                    if cursor.description is not None
                    else None
                )
                affected_rows = (
                    cursor.rowcount
                    if cursor.rowcount >= 0
                    else max(connection.total_changes - before_changes, 0)
                )
                last_insert_rowid = cursor.lastrowid if policy.saw_user_insert else None
                return ExecuteResult(
                    affected_rows=int(affected_rows),
                    last_insert_rowid=(
                        int(last_insert_rowid)
                        if last_insert_rowid is not None
                        else None
                    ),
                    rows=rows,
                ), policy
            finally:
                cursor.close()
        except DashboardDatabaseError:
            raise
        except Exception as error:  # noqa: BLE001 - normalize the public boundary.
            self._raise_public(error, sql, policy=policy)
        finally:
            connection.set_authorizer(None)

    def _raise_public(
        self,
        error: Exception,
        sql: Any,
        *,
        policy: _PolicyState | None = None,
    ) -> None:
        trace_id = uuid.uuid4().hex
        safe_sql = sql if isinstance(sql, str) else f"<{type(sql).__name__}>"
        formatted_traceback = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        self.logger.error(
            "database helper SQL failure traceId=%s sql=%r exception_type=%s\n%s",
            trace_id,
            safe_sql,
            type(error).__name__,
            formatted_traceback,
        )

        failure = _classify_error(error, policy)
        raise DashboardDatabaseError(
            failure.code,
            failure.message,
            retryable=failure.retryable,
            trace_id=trace_id,
            recovery_hint=failure.recovery_hint,
            details={"exception_type": type(error).__name__, **failure.details},
        ) from error


_active_transactions: contextvars.ContextVar[tuple[int, ...]] = contextvars.ContextVar(
    "toolframe_dashboard_active_transactions",
    default=(),
)
_dashboard_request_metadata: contextvars.ContextVar[Mapping[str, Any]] = (
    contextvars.ContextVar(
        "toolframe_dashboard_request_metadata", default=MappingProxyType({})
    )
)


class _DashboardTransaction:
    def __init__(self, database: DashboardDatabase) -> None:
        self._database = database
        self._connection: sqlite3.Connection | None = None
        self._context_token: contextvars.Token[tuple[int, ...]] | None = None
        self._failed = False
        self._entered = False
        self._transaction_id = uuid.uuid4().hex
        self._operation_id: str | None = None
        self._started_at: float | None = None

    def __enter__(self) -> Self:
        operation_id = uuid.uuid4().hex
        self._started_at = time.monotonic()
        self._operation_id = operation_id
        self._database._emit_audit(
            "dashboard_transaction_started",
            operationId=operation_id,
            transactionId=self._transaction_id,
        )
        if self._entered or id(self._database) in _active_transactions.get():
            error = DashboardDatabaseError(
                "nested_transaction",
                "Dashboard database transactions cannot be nested.",
                recovery_hint="Use the existing transaction object for every statement.",
            )
            self._database._emit_audit(
                "dashboard_transaction_rolled_back",
                level="WARNING",
                operationId=operation_id,
                transactionId=self._transaction_id,
                outcome="rejected",
                durationMs=_duration_ms(self._started_at),
                retryable=error.retryable,
                error=_error_payload(error),
                errorTraceId=error.trace_id,
            )
            raise error

        self._entered = True
        active = _active_transactions.get()
        self._context_token = _active_transactions.set((*active, id(self._database)))
        try:
            self._connection = self._database._connect(read_only=False)
            self._connection.execute("BEGIN")
            return self
        except Exception as error:  # noqa: BLE001 - normalize the public boundary.
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            self._reset_context()
            try:
                self._database._raise_public(error, "BEGIN")
            except DashboardDatabaseError as public_error:
                self._database._emit_audit(
                    "dashboard_transaction_rolled_back",
                    level="ERROR",
                    operationId=operation_id,
                    transactionId=self._transaction_id,
                    outcome="failed",
                    durationMs=_duration_ms(self._started_at),
                    retryable=public_error.retryable,
                    error=_error_payload(public_error),
                    errorTraceId=public_error.trace_id,
                )
                raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool:
        connection = self._connection
        self._connection = None
        try:
            if connection is None:
                return False
            if exc_type is None and not self._failed:
                try:
                    connection.commit()
                except Exception as error:  # noqa: BLE001 - normalize the public boundary.
                    _rollback_quietly(connection, self._database.logger)
                    try:
                        self._database._raise_public(error, "COMMIT")
                    except DashboardDatabaseError as public_error:
                        self._database._emit_audit(
                            "dashboard_transaction_rolled_back",
                            level="ERROR",
                            operationId=self._operation_id,
                            transactionId=self._transaction_id,
                            outcome="failed",
                            durationMs=_duration_ms(self._started_at),
                            retryable=public_error.retryable,
                            error=_error_payload(public_error),
                            errorTraceId=public_error.trace_id,
                        )
                        raise
                self._database._emit_audit(
                    "dashboard_transaction_committed",
                    operationId=self._operation_id,
                    transactionId=self._transaction_id,
                    outcome="success",
                    durationMs=_duration_ms(self._started_at),
                )
            else:
                _rollback_quietly(connection, self._database.logger)
                outcome = "failed" if self._failed else "cancelled"
                error_payload = (
                    _error_payload(exc_value)
                    if isinstance(exc_value, DashboardDatabaseError)
                    else {
                        "type": exc_type.__name__,
                        "message": str(exc_value),
                        "traceId": None,
                        "traceback": "".join(
                            traceback.format_exception(exc_type, exc_value, exc_tb)
                        ),
                    }
                    if exc_type is not None
                    else None
                )
                self._database._emit_audit(
                    "dashboard_transaction_rolled_back",
                    level="WARNING" if outcome == "cancelled" else "ERROR",
                    operationId=self._operation_id,
                    transactionId=self._transaction_id,
                    outcome=outcome,
                    durationMs=_duration_ms(self._started_at),
                    retryable=False,
                    error=error_payload,
                    errorTraceId=(
                        exc_value.trace_id
                        if isinstance(exc_value, DashboardDatabaseError)
                        else None
                    ),
                )
                if exc_type is None and self._failed:
                    raise DashboardDatabaseError(
                        "transaction_aborted",
                        "The transaction was rolled back after an earlier database failure.",
                        recovery_hint="Fix the first transaction error and retry the whole transaction.",
                    )
            return False
        finally:
            if connection is not None:
                connection.close()
            self._reset_context()

    def query(
        self,
        sql: str,
        params: Mapping[str, Any] | Sequence[Any] = (),
        *,
        max_rows: int | None = None,
        max_bytes: int | None = None,
    ) -> pd.DataFrame:
        operation_id = uuid.uuid4().hex
        started_at = time.monotonic()
        self._database._emit_audit(
            "dashboard_transaction_statement_started",
            payload=_statement_payload(sql, params),
            operationId=operation_id,
            transactionId=self._transaction_id,
            method="query",
        )
        try:
            connection = self._require_open()
            result = self._database._query_on_connection(
                connection,
                sql,
                params,
                max_rows=max_rows,
                max_bytes=max_bytes,
            )
            self._database._emit_audit(
                "dashboard_transaction_statement_completed",
                payload={"rows": _dataframe_records(result)},
                operationId=operation_id,
                transactionId=self._transaction_id,
                method="query",
                outcome="success",
                durationMs=_duration_ms(started_at),
            )
            return result
        except DashboardDatabaseError as error:
            self._failed = True
            self._database._emit_statement_failure(
                operation_id,
                error,
                started_at=started_at,
                transaction_id=self._transaction_id,
                event="dashboard_transaction_statement_completed",
                method="query",
            )
            raise

    def execute(
        self,
        sql: str,
        params: Mapping[str, Any] | Sequence[Any] = (),
    ) -> ExecuteResult:
        operation_id = uuid.uuid4().hex
        started_at = time.monotonic()
        self._database._emit_audit(
            "dashboard_transaction_statement_started",
            payload=_statement_payload(sql, params),
            operationId=operation_id,
            transactionId=self._transaction_id,
            method="execute",
        )
        try:
            connection = self._require_open()
            result, policy = self._database._execute_on_connection(
                connection, sql, params
            )
            self._database._emit_audit(
                "dashboard_transaction_statement_completed",
                payload=_execute_result_payload(result, policy),
                operationId=operation_id,
                transactionId=self._transaction_id,
                method="execute",
                outcome="success",
                durationMs=_duration_ms(started_at),
            )
            return result
        except DashboardDatabaseError as error:
            self._failed = True
            self._database._emit_statement_failure(
                operation_id,
                error,
                started_at=started_at,
                transaction_id=self._transaction_id,
                event="dashboard_transaction_statement_completed",
                method="execute",
            )
            raise

    def _require_open(self) -> sqlite3.Connection:
        if self._connection is None:
            raise DashboardDatabaseError(
                "transaction_closed",
                "The dashboard database transaction is not active.",
                recovery_hint="Use the transaction object only inside its with block.",
            )
        if self._failed:
            raise DashboardDatabaseError(
                "transaction_aborted",
                "The transaction cannot continue after a database failure.",
                recovery_hint="Leave the with block and retry the whole transaction.",
            )
        return self._connection

    def _reset_context(self) -> None:
        if self._context_token is not None:
            _active_transactions.reset(self._context_token)
            self._context_token = None


def _validate_hard_limit(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _validate_table_prefix(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("protected table prefixes must be non-empty strings")
    return value.casefold()


def _validate_call_limit(name: str, value: int | None, hard_limit: int) -> int:
    if value is None:
        return hard_limit
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > hard_limit
    ):
        raise _SdkFailure(
            "invalid_limit",
            f"{name} must be a positive integer no greater than the configured limit.",
            recovery_hint="Choose a smaller positive per-query limit.",
            details={"limit": hard_limit},
        )
    return value


def _validate_sql(sql: Any) -> str:
    if not isinstance(sql, str):
        raise _SdkFailure(
            "invalid_sql",
            "SQL must be supplied as text.",
            recovery_hint="Pass one SQL statement as a string.",
        )
    normalized = sql.strip()
    if not normalized or not _has_executable_sql(normalized):
        raise _SdkFailure(
            "empty_sql",
            "SQL must not be empty.",
            recovery_hint="Pass one non-empty SQL statement.",
        )
    encoded_size = len(normalized.encode("utf-8"))
    if encoded_size > _MAX_SQL_BYTES:
        raise _SdkFailure(
            "sql_too_long",
            "SQL exceeds the database-helper statement size limit.",
            recovery_hint="Shorten the SQL statement.",
            details={"limit_bytes": _MAX_SQL_BYTES},
        )
    return normalized


def _validate_params(
    params: Any,
    *,
    max_bytes: int,
) -> Mapping[str, Any] | Sequence[Any]:
    if isinstance(params, Mapping):
        if not all(isinstance(key, str) for key in params):
            raise _invalid_parameters(
                "Named SQL parameter keys must be strings.",
                "Use placeholder names without the ':', '@', or '$' prefix as mapping keys.",
            )
        normalized_params: Mapping[str, Any] | Sequence[Any] = dict(params)
        values = tuple(normalized_params.values())
    elif isinstance(params, Sequence) and not isinstance(
        params, (str, bytes, bytearray, memoryview)
    ):
        normalized_params = tuple(params)
        values = normalized_params
    else:
        raise _invalid_parameters(
            "SQL parameters must be a mapping or a concrete non-text sequence.",
            "Pass named parameters as a mapping or positional parameters as a tuple/list.",
        )

    measured_bytes = 0
    for value in values:
        if value is None or isinstance(value, bool):
            measured_bytes += 1
        elif isinstance(value, int):
            if not -(2**63) <= value < 2**63:
                raise _invalid_parameters(
                    "An integer SQL parameter is outside SQLite's signed 64-bit range.",
                    "Store larger numeric identifiers as validated text.",
                )
            measured_bytes += 8
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise _invalid_parameters(
                    "Floating-point SQL parameters must be finite.",
                    "Validate NaN and infinity before calling the database helper.",
                )
            measured_bytes += 8
        elif isinstance(value, str):
            measured_bytes += len(value.encode("utf-8"))
        elif isinstance(value, (bytes, bytearray)):
            measured_bytes += len(value)
        elif isinstance(value, memoryview):
            measured_bytes += value.nbytes
        else:
            raise _invalid_parameters(
                "A SQL parameter has an unsupported scalar type.",
                "Bind only None, bool, signed 64-bit int, finite float, text, or bytes.",
            )
        if measured_bytes > max_bytes:
            raise _invalid_parameters(
                "SQL parameters exceed the database-helper byte limit.",
                "Submit smaller text/blob values or fewer parameters.",
                details={"limit_bytes": max_bytes},
            )
    return normalized_params


def _invalid_parameters(
    message: str,
    recovery_hint: str,
    *,
    details: Mapping[str, Any] | None = None,
) -> _SdkFailure:
    return _SdkFailure(
        "invalid_parameters",
        message,
        recovery_hint=recovery_hint,
        details=details,
    )


def _has_executable_sql(sql: str) -> bool:
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


def _leading_keyword(sql: str) -> str:
    """Return the first SQL token while skipping whitespace and comments."""

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
        break
    end = index
    while end < len(sql) and (sql[end].isalnum() or sql[end] == "_"):
        end += 1
    return sql[index:end].upper()


def _dataframe_from_cursor(
    cursor: sqlite3.Cursor,
    max_rows: int,
    max_bytes: int,
) -> pd.DataFrame:
    columns = [str(column[0]) for column in cursor.description or ()]
    measured_bytes = sum(len(column.encode("utf-8")) for column in columns)
    rows: list[tuple[Any, ...]] = []

    while True:
        row = cursor.fetchone()
        if row is None:
            break
        if len(rows) >= max_rows:
            raise _SdkFailure(
                "result_too_large",
                "The SQL result exceeds the database-helper row limit.",
                recovery_hint="Filter, aggregate, or paginate the query.",
                details={"limit_rows": max_rows},
            )
        measured_bytes += sum(_value_size(value) for value in row)
        if measured_bytes > max_bytes:
            raise _SdkFailure(
                "result_too_large",
                "The SQL result exceeds the database-helper byte limit.",
                recovery_hint="Select fewer columns or reduce large text/blob values.",
                details={"limit_bytes": max_bytes},
            )
        rows.append(tuple(row))

    frame = pd.DataFrame.from_records(rows, columns=columns)
    frame_bytes = int(frame.memory_usage(index=True, deep=True).sum())
    if frame_bytes > max_bytes:
        raise _SdkFailure(
            "result_too_large",
            "The SQL result exceeds the database-helper byte limit.",
            recovery_hint="Select fewer columns or reduce large text/blob values.",
            details={"limit_bytes": max_bytes},
        )
    return frame


def _value_size(value: Any) -> int:
    if value is None:
        return 1
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return len(value)
    if isinstance(value, float) and not math.isfinite(value):
        return 8
    if isinstance(value, (int, float, bool)):
        return 8
    return len(repr(value).encode("utf-8"))


def _classify_error(error: Exception, policy: _PolicyState | None) -> _SdkFailure:
    if isinstance(error, _SdkFailure):
        return error

    if policy is not None and policy.denied_code is not None:
        return _SdkFailure(
            policy.denied_code,
            policy.denied_message or "The SQL statement was denied.",
            recovery_hint=policy.denied_hint or "Use a permitted SQL statement.",
        )

    error_text = str(error).casefold()
    if isinstance(error, sqlite3.IntegrityError):
        return _SdkFailure(
            "constraint_violation",
            "The operation violated a database constraint.",
            recovery_hint="Correct the submitted data or resolve the conflicting row.",
        )
    if isinstance(error, sqlite3.Error) and (
        "locked" in error_text or "busy" in error_text
    ):
        return _SdkFailure(
            "database_busy",
            "The database is busy or locked.",
            retryable=True,
            recovery_hint="Wait briefly for the other operation to finish, then retry once.",
        )
    if isinstance(error, sqlite3.Error) and any(
        marker in error_text
        for marker in ("disk i/o error", "database or disk is full")
    ):
        return _SdkFailure(
            "database_io",
            "The database storage is temporarily unavailable or full.",
            retryable=True,
            recovery_hint="Check storage availability or free disk space, then retry once.",
        )
    if isinstance(error, sqlite3.Error) and (
        "one statement at a time" in error_text
        or "only execute one statement" in error_text
    ):
        return _SdkFailure(
            "multi_statement",
            "Only one SQL statement may be executed per call.",
            recovery_hint="Split the operation into separate helper calls or a transaction block.",
        )
    if isinstance(error, sqlite3.Error) and any(
        marker in error_text
        for marker in ("binding", "bindings", "parameter", "unsupported type")
    ):
        return _SdkFailure(
            "invalid_parameters",
            "The SQL parameter bindings are invalid.",
            recovery_hint="Match every placeholder with a supported bound value.",
        )
    if isinstance(error, sqlite3.Error) and any(
        marker in error_text
        for marker in ("syntax error", "incomplete input", "unrecognized token")
    ):
        return _SdkFailure(
            "syntax_error",
            "The SQL statement has invalid SQLite syntax.",
            recovery_hint="Correct the statement in the widget source.",
        )
    if isinstance(error, sqlite3.Error) and any(
        marker in error_text for marker in ("too big", "too large")
    ):
        return _SdkFailure(
            "result_too_large",
            "The SQL result exceeds the database-helper byte limit.",
            recovery_hint="Select fewer columns or reduce large text/blob values.",
        )
    if isinstance(error, sqlite3.Error) and any(
        marker in error_text
        for marker in ("unable to open database", "not a database", "malformed")
    ):
        return _SdkFailure(
            "database_unavailable",
            "The configured database is unavailable.",
            recovery_hint="Inspect the server log and repair or restore data/data.db.",
        )
    if isinstance(error, sqlite3.Error):
        return _SdkFailure(
            "database_error",
            "The database could not execute the statement.",
            recovery_hint="Inspect the server log and correct the widget query or schema.",
        )
    return _SdkFailure(
        "internal_error",
        "The database helper encountered an unexpected error.",
        recovery_hint="Inspect the server log using the trace ID.",
    )


def _rollback_quietly(connection: sqlite3.Connection, logger: logging.Logger) -> None:
    try:
        connection.rollback()
    except Exception:
        logger.exception("database helper rollback failed")


def _statement_payload(
    sql: Any, params: Mapping[str, Any] | Sequence[Any] | Any
) -> dict[str, Any]:
    if isinstance(params, Mapping):
        parameter_payload: Any = {
            str(key): _audit_value(value) for key, value in params.items()
        }
    elif isinstance(params, Sequence) and not isinstance(
        params, (str, bytes, bytearray, memoryview)
    ):
        parameter_payload = [_audit_value(value) for value in params]
    else:
        parameter_payload = _audit_value(params)
    return {"sql": _audit_value(sql), "parameters": parameter_payload}


def _audit_value(value: Any, *, _seen: set[int] | None = None) -> Any:
    if value is None or isinstance(value, (str, bytes, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else {"nonFiniteFloat": repr(value)}
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    seen = _seen if _seen is not None else set()
    identity = id(value)
    if identity in seen:
        return {"cyclicReference": type(value).__name__}
    seen.add(identity)
    try:
        if isinstance(value, Mapping):
            return {
                str(key): _audit_value(item, _seen=seen) for key, item in value.items()
            }
        if isinstance(value, Sequence):
            return [_audit_value(item, _seen=seen) for item in value]
        return {"unsupportedType": type(value).__name__, "repr": repr(value)}
    finally:
        seen.remove(identity)


def _dataframe_records(frame: pd.DataFrame | None) -> list[dict[str, Any]] | None:
    if frame is None:
        return None
    return [
        {str(key): _audit_value(value) for key, value in row.items()}
        for row in frame.to_dict("records")
    ]


def _execute_result_payload(
    result: ExecuteResult, policy: _PolicyState
) -> dict[str, Any]:
    return {
        "affectedRows": result.affected_rows,
        "lastInsertRowId": result.last_insert_rowid,
        "rows": _dataframe_records(result.rows),
        "triggeredWrites": policy.saw_trigger_write,
    }


def _error_payload(error: DashboardDatabaseError) -> dict[str, Any]:
    return {
        "type": type(error).__name__,
        "code": error.code,
        "message": error.message,
        "traceId": error.trace_id,
        "details": dict(error.details),
        "traceback": "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        ),
    }


def _duration_ms(started_at: float | None) -> float:
    if started_at is None:
        return 0.0
    return round((time.monotonic() - started_at) * 1000, 3)


_configuration_lock = threading.RLock()
_configuration: _SdkConfiguration | None = None
_current_context: contextvars.ContextVar[DashboardContext | None] = (
    contextvars.ContextVar(
        "toolframe_dashboard_context",
        default=None,
    )
)


def _configure(
    *,
    database_path: Path,
    workspace: Path,
    logger: logging.Logger,
    runtime: Mapping[str, Any],
    app_dir: Path,
    domains_dir: Path,
    audit_sink: Any | None = None,
    artifacts: ArtifactStore | None = None,
) -> DashboardContext:
    """Configure the process-global SDK before preflight and HTTP readiness."""

    global _configuration
    app_candidate = Path(app_dir).expanduser().absolute()
    domains_candidate = Path(domains_dir).expanduser().absolute()
    domains_path = _real_directory(domains_candidate, "domains directory")
    app_path = _real_directory(app_candidate, "app directory")
    workspace_path = Path(workspace).expanduser().resolve(strict=False)
    if artifacts is None:
        # The launcher prepares this directory before configuration. Creating
        # it here preserves the standalone SDK/test configuration contract.
        workspace_path.mkdir(parents=True, exist_ok=True)
        artifacts = ArtifactStore(workspace_path, audit_sink=audit_sink, logger=logger)
    frozen_runtime = _freeze(runtime)
    if not isinstance(frozen_runtime, Mapping):
        raise TypeError("runtime must be a mapping")
    context = DashboardContext(
        db=DashboardDatabase(database_path, logger=logger, audit_sink=audit_sink),
        workspace=workspace_path,
        logger=logger,
        widget_id="",
        runtime=frozen_runtime,
        artifacts=artifacts,
    )
    with _configuration_lock:
        _configuration = _SdkConfiguration(app_path, domains_path, context)
        _ensure_import_path(app_path)
        _ensure_domain_namespace(domains_path)
    return context


def get_context() -> DashboardContext:
    """Return the current widget context, or the configured base context."""

    current = _current_context.get()
    if current is not None:
        return current
    with _configuration_lock:
        if _configuration is None:
            raise RuntimeError("toolframe_dashboard is not configured")
        return _configuration.base_context


def preflight_app(
    app_dir: Path | None = None,
    domains_dir: Path | None = None,
) -> tuple[DashboardWidget, ...]:
    """Validate all public Python sources and import every domain widget."""

    app_root, domains_root = _resolve_app_layout(app_dir, domains_dir)
    validate_source_architecture(app_root, domains_root)
    return discover_domain_widgets(domains_root)


def discover_widgets() -> tuple[DashboardWidget, ...]:
    """Reload and validate every public domain UI widget."""

    return discover_domain_widgets()


def discover_domain_widgets(
    domains_dir: Path | None = None,
) -> tuple[DashboardWidget, ...]:
    """Reload and validate every direct public ``domains/*/ui/*.py`` widget."""

    if domains_dir is None:
        with _configuration_lock:
            if _configuration is None:
                raise RuntimeError("toolframe domain UI is not configured")
            domains_root = _configuration.domains_dir
    else:
        domains_root = _real_directory(
            Path(domains_dir).expanduser().absolute(), "domains directory"
        )
    layouts = _discover_domain_layouts(domains_root)

    widgets: list[DashboardWidget] = []
    with _configuration_lock:
        importlib.invalidate_caches()
        _purge_domain_modules(domains_root)
        _ensure_domain_namespace(domains_root)
        for domain, domain_root, ui_directory in layouts:
            try:
                importlib.import_module(f"domains.{domain}.ui")
            except Exception as error:
                raise RuntimeError(
                    f"domain {domain} public API or ui package failed import: "
                    f"{type(error).__name__}: {error}"
                ) from error
            for path in _public_widget_paths(ui_directory):
                widgets.append(
                    _load_widget(
                        path,
                        f"domains.{domain}.ui",
                        widget_id=f"{domain}.{path.stem}",
                        domain=domain,
                    )
                )

    explicit_orders: dict[int, Path] = {}
    for widget in widgets:
        if "ORDER" not in vars(widget.module):
            continue
        previous = explicit_orders.setdefault(widget.order, widget.path)
        if previous != widget.path:
            raise RuntimeError(
                "domain widget ORDER values must be globally unique: "
                f"{previous} and {widget.path} both use {widget.order}"
            )
    return tuple(
        sorted(
            widgets,
            key=lambda widget: (
                widget.order,
                widget.domain,
                widget.path.name,
            ),
        )
    )


def render_widget(
    widget: DashboardWidget,
    context: DashboardContext | None = None,
) -> None:
    """Render one validated widget; unhandled widget exceptions propagate."""

    base_context = context or get_context()
    inherited_metadata = _dashboard_request_metadata.get()
    owns_request_metadata = not inherited_metadata
    request_metadata = (
        _collect_dashboard_request_metadata()
        if owns_request_metadata and base_context.db.audit_sink is not None
        else dict(inherited_metadata)
    )
    request_metadata["widget"] = widget.widget_id
    widget_context = replace(
        base_context,
        widget_id=widget.widget_id,
        logger=base_context.logger.getChild(widget.widget_id),
    )
    token = _current_context.set(widget_context)
    metadata_token = _dashboard_request_metadata.set(request_metadata)
    try:
        widget.module.render(widget_context)
    finally:
        _dashboard_request_metadata.reset(metadata_token)
        _current_context.reset(token)


def render_widgets(
    widgets: Iterable[DashboardWidget] | None = None,
    context: DashboardContext | None = None,
) -> None:
    """Discover (when omitted) and render widgets in validated order."""

    selected = discover_widgets() if widgets is None else tuple(widgets)
    base_context = context or get_context()
    metadata = (
        _collect_dashboard_request_metadata()
        if base_context.db.audit_sink is not None
        else {}
    )
    metadata_token = _dashboard_request_metadata.set(metadata)
    try:
        for widget in selected:
            render_widget(widget, base_context)
    finally:
        _dashboard_request_metadata.reset(metadata_token)


def _collect_dashboard_request_metadata() -> dict[str, Any]:
    dashboard_session_id = uuid.uuid4().hex
    source_ip: str | None = None
    user_agent: str | None = None
    try:
        import streamlit as st

        try:
            existing_session_id = st.session_state.get(_DASHBOARD_SESSION_STATE_KEY)
            if not isinstance(existing_session_id, str) or not existing_session_id:
                st.session_state[_DASHBOARD_SESSION_STATE_KEY] = dashboard_session_id
            else:
                dashboard_session_id = existing_session_id
        except Exception:  # noqa: BLE001 - Streamlit context may be absent in tests.
            dashboard_session_id = uuid.uuid4().hex

        try:
            candidate_ip = st.context.ip_address
            source_ip = candidate_ip if isinstance(candidate_ip, str) else None
            headers = st.context.headers
            candidate_agent = headers.get("User-Agent")
            user_agent = candidate_agent if isinstance(candidate_agent, str) else None
        except Exception:  # noqa: BLE001 - public context is runtime-only.
            source_ip = None
            user_agent = None
    except ImportError:
        source_ip = None
    return {
        "dashboardSessionId": dashboard_session_id,
        "requestId": uuid.uuid4().hex,
        "sourceIp": source_ip,
        "userAgent": user_agent,
        "widget": None,
    }


def _resolve_app_layout(
    app_dir: Path | None, domains_dir: Path | None
) -> tuple[Path, Path]:
    if app_dir is None and domains_dir is None:
        with _configuration_lock:
            if _configuration is None or _configuration.domains_dir is None:
                raise RuntimeError("toolframe app and domains are not configured")
            return _configuration.app_dir, _configuration.domains_dir
    if app_dir is None or domains_dir is None:
        raise RuntimeError("app_dir and domains_dir must be supplied together")
    return (
        _real_directory(Path(app_dir).expanduser().absolute(), "app directory"),
        _real_directory(Path(domains_dir).expanduser().absolute(), "domains directory"),
    )


def _real_directory(path: Path, label: str) -> Path:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RuntimeError(f"could not inspect {label} {path}: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(f"{label} must be a real directory, not a symlink: {path}")
    return path.resolve(strict=True)


def _real_file(path: Path, label: str) -> Path:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RuntimeError(f"could not inspect {label} {path}: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"{label} must be a regular file, not a symlink: {path}")
    return path.resolve(strict=True)


def _ensure_import_path(root: Path) -> None:
    root_text = os.fspath(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)


def _ensure_domain_namespace(domains_root: Path) -> None:
    module = ModuleType("domains")
    module.__package__ = "domains"
    module.__path__ = [os.fspath(domains_root)]
    module.__spec__ = importlib.util.spec_from_loader(
        "domains", loader=None, is_package=True
    )
    sys.modules["domains"] = module


def _discover_domain_layouts(root: Path) -> list[tuple[str, Path, Path]]:
    layouts: list[tuple[str, Path, Path]] = []
    try:
        entries = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError as error:
        raise RuntimeError(
            f"could not read domains directory {root}: {error}"
        ) from error
    for path in entries:
        name = path.name
        if name.startswith((".", "_")):
            continue
        if not _DOMAIN_NAME.fullmatch(name) or keyword.iskeyword(name):
            raise RuntimeError(
                f"domain name must be a lower_snake_case Python identifier: {path}"
            )
        domain_root = _real_directory(path, f"domain {name}")
        _real_file(domain_root / "__init__.py", f"domain {name} public API")
        for child in _DOMAIN_DIRECTORIES:
            _real_directory(domain_root / child, f"domain {name} {child}")
        layouts.append((name, domain_root, domain_root / "ui"))
    if not layouts:
        raise RuntimeError(
            f"domains directory must contain at least one public business domain: {root}"
        )
    return layouts


def _public_widget_paths(directory: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    try:
        entries = sorted(directory.iterdir(), key=lambda path: path.name)
    except OSError as error:
        raise RuntimeError(
            f"could not read domain ui directory {directory}: {error}"
        ) from error
    for path in entries:
        if path.name.startswith((".", "_")) or path.suffix != ".py":
            continue
        if not _WIDGET_NAME.fullmatch(path.stem) or keyword.iskeyword(path.stem):
            raise RuntimeError(
                f"domain widget filename must be lower_snake_case: {path}"
            )
        _real_file(path, "domain widget")
        paths.append(path.resolve(strict=True))
    return tuple(paths)


def _purge_domain_modules(root: Path) -> None:
    for name, module in tuple(sys.modules.items()):
        if name == "domains" or name.startswith("domains."):
            sys.modules.pop(name, None)
            continue
        module_path = getattr(module, "__file__", None)
        if not module_path:
            continue
        try:
            Path(module_path).resolve(strict=False).relative_to(root)
        except (OSError, ValueError):
            continue
        sys.modules.pop(name, None)


def _load_widget(
    path: Path,
    package_name: str,
    *,
    widget_id: str,
    domain: str,
) -> DashboardWidget:
    if path.stem.isidentifier() and not keyword.iskeyword(path.stem):
        module_component = path.stem
    else:
        filename_hash = hashlib.sha256(os.fsencode(path.name)).hexdigest()[:16]
        module_component = f"_widget_{filename_hash}"
    module_name = f"{package_name}.{module_component}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None:
        raise RuntimeError(
            f"could not create an import spec for dashboard widget: {path}"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        code = compile(path.read_bytes(), os.fspath(path), "exec")
        exec(code, module.__dict__)  # noqa: S102 - widgets are trusted local Python.
        _validate_widget_module(path, module)
    except Exception as error:
        sys.modules.pop(module_name, None)
        raise RuntimeError(
            f"dashboard widget {path.name} failed import/contract validation: "
            f"{type(error).__name__}: {error}"
        ) from error

    title = vars(module).get("TITLE", path.stem.replace("_", " ").title())
    order = vars(module).get("ORDER", _DEFAULT_WIDGET_ORDER)
    return DashboardWidget(
        widget_id,
        title.strip(),
        order,
        path,
        module_name,
        module,
        domain,
    )


def _validate_widget_module(path: Path, module: ModuleType) -> None:
    namespace = vars(module)
    render = namespace.get("render")
    if not inspect.isfunction(render):
        raise RuntimeError(
            f"dashboard widget {path.name} must export a render(context) function"
        )
    if (
        inspect.iscoroutinefunction(render)
        or inspect.isgeneratorfunction(render)
        or inspect.isasyncgenfunction(render)
    ):
        raise RuntimeError(
            f"dashboard widget {path.name} render(context) must be synchronous"
        )
    parameters = tuple(inspect.signature(render).parameters.values())
    if len(parameters) != 1 or parameters[0].kind not in {
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    }:
        raise RuntimeError(
            f"dashboard widget {path.name} render(context) must accept exactly one positional parameter"
        )

    if "TITLE" in namespace:
        title = namespace["TITLE"]
        if not isinstance(title, str) or not title.strip():
            raise RuntimeError(
                f"dashboard widget {path.name} TITLE must be a non-empty string"
            )
    if "ORDER" in namespace:
        order = namespace["ORDER"]
        if isinstance(order, bool) or not isinstance(order, int):
            raise RuntimeError(f"dashboard widget {path.name} ORDER must be an integer")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value
