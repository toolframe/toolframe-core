"""PostgreSQL implementation of Toolframe's shared database operations."""

from __future__ import annotations

import hashlib
import math
import re
import uuid
from collections.abc import Mapping

import psycopg
import sqlglot
from sqlalchemy import create_engine
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlglot import exp
from sqlglot.tokens import TokenType
from toolframe_database import (
    DashboardDatabaseError,
    DatabaseOperations,
    ExecuteResult,
    _dataframe_from_cursor,
    _PolicyState,
    _protect_private_tables,
    _SdkFailure,
    _validate_call_limit,
    _validate_params,
    _validate_sql,
)


def _failure(code, message, *, retryable=False):
    return _SdkFailure(
        code,
        message,
        retryable=retryable,
        recovery_hint="Check the operation and configured database, then retry if indicated.",
    )


def _bind(sql, params):
    """Convert public bindings without touching literals, comments, or casts."""
    tokens = sqlglot.Dialect.get_or_raise("postgres").tokenize(sql)
    replacements = []
    mapping = isinstance(params, Mapping)
    for index, token in enumerate(tokens):
        if mapping and token.token_type == TokenType.COLON and index + 1 < len(tokens):
            name = tokens[index + 1]
            if name.start == token.end + 1 and re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*", name.text
            ):
                if name.text not in params:
                    raise _failure(
                        "invalid_parameters", "A named SQL binding is missing."
                    )
                replacements.append((token.start, name.end + 1, f"%({name.text})s"))
        elif not mapping and token.token_type == TokenType.PLACEHOLDER:
            replacements.append((token.start, token.end + 1, "%s"))
    if not mapping and len(replacements) != len(params):
        raise _failure(
            "invalid_parameters",
            "The SQL placeholder count does not match its bindings.",
        )
    if not replacements and not params:
        return sql, None
    parts, start = [], 0
    for begin, end, replacement in replacements:
        parts.extend((sql[start:begin].replace("%", "%%"), replacement))
        start = end
    parts.append(sql[start:].replace("%", "%%"))
    return "".join(parts), params


def _validate_statement(sql, *, mode, allow_schema_changes, protected_prefixes):
    try:
        statements = [
            node for node in sqlglot.parse(sql, read="postgres") if node is not None
        ]
    except sqlglot.errors.SqlglotError:
        raise _failure(
            "syntax_error", "The SQL statement has invalid PostgreSQL syntax."
        ) from None
    if len(statements) != 1:
        raise _failure(
            "multi_statement", "Only one SQL statement may be executed per call."
        )
    tree = statements[0]
    is_query = isinstance(tree, (exp.Query, exp.Values))
    writes = (
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Merge,
        exp.Create,
        exp.Drop,
        exp.Alter,
    )
    if mode == "query" and (
        not is_query
        or any(isinstance(node, (*writes, exp.Into, exp.Lock)) for node in tree.walk())
    ):
        raise _failure("read_only_required", "query() accepts read-only SQL only.")
    if mode == "execute" and is_query:
        raise _failure(
            "write_statement_required", "execute() requires a data mutation."
        )
    if isinstance(tree, (exp.Create, exp.Drop, exp.Alter)) and not allow_schema_changes:
        raise _failure(
            "schema_change_forbidden",
            "Schema changes require a plugin migration and restart.",
        )
    if not is_query and not isinstance(tree, writes):
        raise _failure(
            "forbidden_sql", "The SQL operation is not allowed by the database policy."
        )
    for table in tree.find_all(exp.Table):
        if table.catalog or table.db not in {"", "public"}:
            raise _failure(
                "protected_table_forbidden",
                "Cross-schema access is not available through the runtime adapter.",
            )
        if any(
            table.name.casefold().startswith(prefix) for prefix in protected_prefixes
        ):
            raise _failure(
                "protected_table_forbidden",
                "Runtime-owned tables cannot be accessed through this adapter.",
            )
    for function in tree.find_all(exp.Func):
        name = (
            function.name.casefold()
            if isinstance(function, exp.Anonymous)
            else function.sql_name().casefold()
        )
        if name.startswith(("pg_", "lo_", "dblink")) or name in {
            "nextval",
            "setval",
            "set_config",
        }:
            raise _failure(
                "forbidden_sql",
                "Administrative database functions are not available through this adapter.",
            )
    # Runtime DDL is only retained for the standalone development compatibility API.
    # Procedural objects and privileged commands must never bypass the provider policy.
    if isinstance(tree, exp.Create) and tree.args.get("kind", "").upper() not in {
        "TABLE",
        "INDEX",
        "VIEW",
    }:
        raise _failure("forbidden_sql", "This schema operation is not supported.")
    return tree


class _Connection:
    def __init__(self, pooled):
        self._pooled = pooled
        self._raw = pooled.driver_connection

    def execute(self, sql, params=()):
        sql, params = _bind(sql, params)
        cursor = self._raw.cursor()
        try:
            cursor.execute(sql, params)
            return cursor
        except BaseException:
            cursor.close()
            raise

    def stream(self, sql, params):
        sql, params = _bind(sql, params)
        cursor = self._raw.cursor(name=f"toolframe_{uuid.uuid4().hex}")
        try:
            cursor.execute(sql, params)
            return cursor
        except BaseException:
            cursor.close()
            raise

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        try:
            self._raw.rollback()
        finally:
            self._pooled.close()


class PostgreSQLProvider(DatabaseOperations):
    """Bounded PostgreSQL operations using independent pooled connections."""

    dialect = "postgresql"
    path = None

    def __init__(
        self,
        settings,
        *,
        logger=None,
        audit_sink=None,
        allow_schema_changes=False,
        protected_table_prefixes=("_toolframe_", "pg_", "sql_"),
    ):
        self._initialize(
            settings,
            logger=logger,
            audit_sink=audit_sink,
            allow_schema_changes=allow_schema_changes,
            protected_table_prefixes=protected_table_prefixes,
        )
        # No connection is acquired before the plugin lifecycle owns cleanup.
        dsn = settings.postgres_dsn.get_secret_value()
        if dsn.startswith("postgresql://"):
            dsn = "postgresql+psycopg://" + dsn[len("postgresql://") :]
        self._engine = create_engine(
            dsn,
            pool_size=settings.pool_size,
            max_overflow=0,
            pool_timeout=settings.pool_timeout_seconds,
            pool_pre_ping=True,
            hide_parameters=True,
            connect_args={"connect_timeout": math.ceil(settings.timeout_seconds)},
        )

    def close(self):
        super().close()
        self._engine.dispose()

    def _connect(self, *, read_only):
        self._ensure_open()
        connection = _Connection(self._engine.raw_connection())
        try:
            connection._raw.autocommit = True
            timeout = str(max(1, int(self.settings.timeout_seconds * 1000)))
            connection.execute(
                "SELECT set_config('statement_timeout', :timeout, false), "
                "set_config('lock_timeout', :timeout, false), "
                "set_config('search_path', 'public', false)",
                {"timeout": timeout},
            ).close()
            if read_only:
                connection.execute("BEGIN READ ONLY").close()
            return connection
        except BaseException:
            connection.close()
            raise

    def begin_locked(self, connection, key):
        connection.execute("BEGIN").close()
        lock_id = int.from_bytes(
            hashlib.sha256(key.encode()).digest()[:8], "big", signed=True
        )
        connection.execute(
            "SELECT pg_advisory_xact_lock(:key)", {"key": lock_id}
        ).close()

    def _query_on_connection(
        self, connection, sql, params, *, max_rows, max_bytes, private=False
    ):
        try:
            sql = _validate_sql(sql)
            params = _validate_params(params, max_bytes=self.max_bytes)
            row_limit = _validate_call_limit("max_rows", max_rows, self.max_rows)
            byte_limit = _validate_call_limit("max_bytes", max_bytes, self.max_bytes)
            _validate_statement(
                sql,
                mode="query",
                allow_schema_changes=self.allow_schema_changes,
                protected_prefixes=self.protected_table_prefixes
                + (("_toolframe_",) if private else ()),
            )
            with connection.stream(sql, params) as cursor:
                return _dataframe_from_cursor(cursor, row_limit, byte_limit)
        except Exception as error:  # noqa: BLE001 - normalize provider failures.
            self._raise_public(error, sql)

    def _execute_on_connection(self, connection, sql, params, *, private=False):
        try:
            sql = _validate_sql(sql)
            if private:
                _protect_private_tables(sql, dialect="postgres")
            params = _validate_params(params, max_bytes=self.max_bytes)
            tree = _validate_statement(
                sql,
                mode="execute",
                allow_schema_changes=self.allow_schema_changes and not private,
                protected_prefixes=self.protected_table_prefixes
                + (("_toolframe_",) if private else ()),
            )
            with connection.execute(sql, params) as cursor:
                rows = (
                    _dataframe_from_cursor(cursor, self.max_rows, self.max_bytes)
                    if cursor.description
                    else None
                )
                result = ExecuteResult(max(cursor.rowcount, 0), None, rows)
            return result, _PolicyState(
                "execute", saw_write=True, saw_user_insert=isinstance(tree, exp.Insert)
            )
        except Exception as error:  # noqa: BLE001 - normalize provider failures.
            self._raise_public(error, sql)

    def _raise_public(self, error, sql, *, policy=None):
        raise self.normalize_error(error, operation="database operation") from None

    def normalize_error(self, error, *, operation):
        """Translate driver failures through the public provider contract."""
        if isinstance(error, DashboardDatabaseError):
            return error
        original = getattr(error, "orig", error)
        state = getattr(original, "sqlstate", None) or ""
        if isinstance(error, _SdkFailure):
            failure = error
        elif state.startswith("23"):
            failure = _failure(
                "constraint_violation", "The operation violated a database constraint."
            )
        elif state in {"40001", "40P01", "55P03", "57014"} or isinstance(
            error, PoolTimeout
        ):
            failure = _failure(
                "database_busy",
                "The database is busy or the operation timed out.",
                retryable=True,
            )
        elif state == "25006":
            failure = _failure(
                "read_only_required", "query() accepts read-only SQL only."
            )
        elif state == "42601":
            failure = _failure(
                "syntax_error", "The SQL statement has invalid PostgreSQL syntax."
            )
        elif state.startswith(("08", "53", "57", "58")) or isinstance(
            original, psycopg.OperationalError
        ):
            failure = _failure(
                "database_unavailable",
                "The configured database is unavailable.",
                retryable=True,
            )
        elif isinstance(error, (sqlglot.errors.SqlglotError, psycopg.ProgrammingError)):
            failure = _failure(
                "invalid_parameters", "The SQL statement or its bindings are invalid."
            )
        else:
            failure = _failure(
                "database_error", "The database could not execute the operation."
            )
        trace_id = uuid.uuid4().hex
        # Driver errors may contain a URL or authentication details. Never log
        # their text, repr, traceback, or chained exception on this boundary.
        self.logger.error(
            "database helper failure traceId=%s exception_type=%s sqlstate=%s",
            trace_id,
            type(original).__name__,
            state,
        )
        return DashboardDatabaseError(
            failure.code,
            failure.message,
            retryable=failure.retryable,
            trace_id=trace_id,
            recovery_hint=failure.recovery_hint,
            details=failure.details,
        )
