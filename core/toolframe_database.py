"""Database operations and lifecycle, independent of dashboard rendering."""

from __future__ import annotations

import contextvars
import logging
import math
import os
import re
import sqlite3
import time
import traceback
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType, TracebackType
from typing import Any, Self
from urllib.parse import quote

import pandas as pd
from toolframe_idempotency import (
    IdempotencyError,
    IdempotencyResult,
    IdempotencyStore,
    IdempotencyTransaction,
)
from toolframe_idempotency import _audit_context as _idempotency_audit_context

_LOGGER = logging.getLogger("toolframe.dashboard")

__all__ = ["DashboardDatabase", "DashboardDatabaseError", "ExecuteResult"]

_DEFAULT_MAX_ROWS = 10_000

_DEFAULT_MAX_BYTES = 10 * 1024 * 1024

_MAX_SQL_BYTES = 1024 * 1024

_BUSY_TIMEOUT_MS = 5_000


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


@dataclass(frozen=True, slots=True)
class ExecuteResult:
    """Result metadata for a committed DML or DDL statement."""

    affected_rows: int
    last_insert_rowid: int | None
    rows: pd.DataFrame | None = None

    @property
    def lastrowid(self) -> int | None:
        """Legacy SQLite alias; portable callers use insert() or RETURNING."""
        return self.last_insert_rowid

    @property
    def rowcount(self) -> int:
        return self.affected_rows


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
            if action == getattr(sqlite3, "SQLITE_READ", -1) and any(
                (argument_one or "").casefold().startswith(prefix)
                for prefix in self.protected_table_prefixes
            ):
                return self.deny(
                    "protected_table_forbidden",
                    "Runtime-owned tables cannot be accessed through this adapter.",
                    "Read and write business tables only.",
                )
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


class DatabaseOperations:
    """Shared bounded operations, audit events, and atomic transaction semantics."""

    def _initialize(
        self,
        settings,
        *,
        logger=None,
        audit_sink=None,
        allow_schema_changes=False,
        protected_table_prefixes=(),
    ):
        self.settings = settings
        self.logger = logger or _LOGGER
        self.max_rows = _validate_hard_limit("max_rows", settings.max_rows)
        self.max_bytes = _validate_hard_limit("max_bytes", settings.max_bytes)
        self.audit_sink = audit_sink
        self.allow_schema_changes = bool(allow_schema_changes)
        self.protected_table_prefixes = tuple(
            _validate_table_prefix(prefix) for prefix in protected_table_prefixes
        )
        self._closed = False
        self._idempotency = None

    @property
    def identity(self):
        return self.settings.identity

    def public_info(self):
        return self.settings.public_info()

    def close(self):
        """Close provider resources without removing data or schema."""
        self._closed = True

    def _ensure_open(self):
        if self._closed:
            raise DashboardDatabaseError(
                "database_closed",
                "The database provider is closed.",
                recovery_hint="Use the currently active database service.",
            )

    def insert(self, table, values, *, id_column="id"):
        return _insert(self, table, values, id_column=id_column)

    @contextmanager
    def connection(self, *, read_only=False):
        """Provider integration boundary; application code uses the bounded methods."""
        connection = None
        try:
            connection = self._connect(read_only=read_only)
        except DashboardDatabaseError:
            raise
        except Exception as error:  # noqa: BLE001 - normalize provider failures.
            self._raise_public(error, "provider operation")
        try:
            yield connection
        finally:
            if connection is not None:
                connection.close()

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
            connection.execute("BEGIN").close()
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
        self._ensure_open()
        if self._idempotency is None:
            self._idempotency = IdempotencyStore(
                provider=self, audit_sink=self.audit_sink, logger=self.logger
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


class SQLiteProvider(DatabaseOperations):
    """SQLite connections, native authorization, and locking behind the shared API."""

    dialect = "sqlite"

    def __init__(
        self,
        path=None,
        *,
        settings=None,
        logger=None,
        max_rows=_DEFAULT_MAX_ROWS,
        max_bytes=_DEFAULT_MAX_BYTES,
        audit_sink=None,
        allow_schema_changes=True,
        protected_table_prefixes=(),
    ):
        from toolframe_database_config import DatabaseSettings

        if settings is None:
            settings = DatabaseSettings(
                sqlite_path=path,
                max_rows=max_rows,
                max_bytes=max_bytes,
                timeout_seconds=_BUSY_TIMEOUT_MS / 1000,
            )
        self.path = settings.sqlite_path
        self._initialize(
            settings,
            logger=logger,
            audit_sink=audit_sink,
            allow_schema_changes=allow_schema_changes,
            protected_table_prefixes=protected_table_prefixes,
        )

    def begin_locked(self, connection, key):
        """Serialize writers on SQLite before reading the durable claim."""
        connection.execute("BEGIN IMMEDIATE").close()

    def _connect(self, *, read_only: bool) -> sqlite3.Connection:
        self._ensure_open()
        timeout_ms = int(self.settings.timeout_seconds * 1000)
        if read_only:
            encoded_path = quote(os.fspath(self.path), safe="/")
            connection = sqlite3.connect(
                f"file:{encoded_path}?mode=ro",
                uri=True,
                timeout=self.settings.timeout_seconds,
                isolation_level=None,
            )
        else:
            connection = sqlite3.connect(
                self.path,
                timeout=self.settings.timeout_seconds,
                isolation_level=None,
            )

        try:
            connection.execute(f"PRAGMA busy_timeout = {timeout_ms}")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.enable_load_extension(False)
            if hasattr(connection, "setconfig") and hasattr(
                sqlite3, "SQLITE_DBCONFIG_DEFENSIVE"
            ):
                connection.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
            if read_only:
                connection.execute("PRAGMA query_only = ON")
            else:
                _configure_wal(connection, self.settings.timeout_seconds)
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
        private: bool = False,
    ) -> pd.DataFrame:
        length_limit = connection.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
        policy = _PolicyState(
            "query",
            allow_schema_changes=self.allow_schema_changes,
            protected_table_prefixes=self.protected_table_prefixes
            + (("_toolframe_", "sqlite_") if private else ()),
        )
        try:
            self._start_statement_timeout(connection)
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
            connection.set_progress_handler(None, 0)
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, length_limit)

    def _execute_on_connection(
        self,
        connection: sqlite3.Connection,
        sql: str,
        params: Mapping[str, Any] | Sequence[Any],
        *,
        private: bool = False,
    ) -> tuple[ExecuteResult, _PolicyState]:
        length_limit = connection.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
        policy = _PolicyState(
            "execute",
            allow_schema_changes=self.allow_schema_changes and not private,
            protected_table_prefixes=self.protected_table_prefixes
            + (("_toolframe_", "sqlite_") if private else ()),
        )
        try:
            self._start_statement_timeout(connection)
            normalized_sql = _validate_sql(sql)
            if private:
                _protect_private_tables(normalized_sql, dialect="sqlite")
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
            connection.set_progress_handler(None, 0)
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, length_limit)

    def _start_statement_timeout(self, connection):
        deadline = time.monotonic() + self.settings.timeout_seconds
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, self.max_bytes)
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)

    def _raise_public(
        self,
        error: Exception,
        sql: Any,
        *,
        policy: _PolicyState | None = None,
    ) -> None:
        if isinstance(error, DashboardDatabaseError):
            raise error
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


DashboardDatabase = SQLiteProvider


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
            self._connection.execute("BEGIN").close()
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

    def insert(self, table, values, *, id_column="id"):
        try:
            self._require_open()
            return _insert(self, table, values, id_column=id_column)
        except DashboardDatabaseError:
            self._failed = True
            raise

    def _reset_context(self) -> None:
        if self._context_token is not None:
            _active_transactions.reset(self._context_token)
            self._context_token = None


def _validate_hard_limit(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z_][a-z0-9_]*", value):
        raise DashboardDatabaseError(
            "invalid_identifier",
            "A table or column identifier is invalid.",
            recovery_hint="Use lowercase letters, digits, and underscores in identifiers.",
        )
    return f'"{value}"'


def _protect_private_tables(sql, *, dialect):
    import sqlglot
    from sqlglot import exp
    from sqlglot.tokens import TokenType

    # Inspect table nodes, not arbitrary column names or string literals.
    if _leading_keyword(sql) == "REINDEX":
        tokens = sqlglot.Dialect.get_or_raise(dialect).tokenize(sql)
        tables = [
            token.text
            for token in tokens[1:]
            if token.token_type != TokenType.SEMICOLON
        ]
    else:
        try:
            trees = sqlglot.parse(sql, read=dialect)
        except sqlglot.errors.SqlglotError:
            return  # The provider reports syntax errors at execution.
        tables = [
            table.name
            for tree in trees
            if tree is not None
            for table in tree.find_all(exp.Table)
        ]
    if any(name.casefold().startswith("_toolframe_") for name in tables):
        raise _SdkFailure(
            "protected_table_forbidden",
            "Runtime-owned tables are private.",
            recovery_hint="Use business tables inside the idempotent callback.",
        )


def _configure_wal(connection, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            cursor = connection.execute("PRAGMA journal_mode = WAL")
            row = cursor.fetchone()
            cursor.close()
            if row is None or str(row[0]).casefold() != "wal":
                raise sqlite3.OperationalError("SQLite could not enter WAL mode")
            return
        except sqlite3.OperationalError as error:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not any(
                word in str(error).casefold() for word in ("busy", "locked")
            ):
                raise
            time.sleep(min(0.01, remaining))


def _insert(database, table, values, *, id_column):
    if not isinstance(values, Mapping) or not values:
        raise DashboardDatabaseError(
            "invalid_parameters",
            "insert() requires a non-empty value mapping.",
            recovery_hint="Pass column names and bound scalar values.",
        )
    names = list(values)
    columns = ", ".join(_identifier(name) for name in names)
    placeholders = ", ".join(f":p{index}" for index in range(len(names)))
    result = database.execute(
        f"INSERT INTO {_identifier(table)} ({columns}) VALUES ({placeholders}) "
        f"RETURNING {_identifier(id_column)}",
        {f"p{index}": values[name] for index, name in enumerate(names)},
    )
    if result.rows is None or len(result.rows) != 1:
        raise DashboardDatabaseError(
            "missing_insert_id",
            "The insert did not return exactly one identifier.",
            recovery_hint="Use an integer identity column and verify the schema.",
        )
    return int(result.rows.iloc[0, 0])


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
        "locked" in error_text or "busy" in error_text or "interrupted" in error_text
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
