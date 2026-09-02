"""Durable, explicit idempotency for local SQLite business mutations."""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import math
import re
import sqlite3
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

__all__ = [
    "IdempotencyError",
    "IdempotencyResult",
    "IdempotencyStore",
    "IdempotencyTransaction",
    "run_once",
]

_LOGGER = logging.getLogger("lite_mcp.idempotency")
_TABLE = "_lite_mcp_idempotency"
_RECEIPT_SCHEMA = """
CREATE TABLE _lite_mcp_idempotency (
    namespace TEXT NOT NULL,
    business_key TEXT NOT NULL,
    input_sha256 TEXT NOT NULL CHECK (length(input_sha256) = 64),
    result_json BLOB NOT NULL,
    result_sha256 TEXT NOT NULL CHECK (length(result_sha256) = 64),
    result_bytes INTEGER NOT NULL CHECK (result_bytes BETWEEN 1 AND 1048576),
    original_operation_id TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ')),
    PRIMARY KEY (namespace, business_key)
) STRICT, WITHOUT ROWID;
"""
_BUSY_TIMEOUT_MS = 5_000
_DEFAULT_INPUT_LIMIT_BYTES = 1024 * 1024
_DURABLE_RESULT_LIMIT_BYTES = 1024 * 1024
_DEFAULT_RESULT_LIMIT_BYTES = _DURABLE_RESULT_LIMIT_BYTES
_MAX_NAMESPACE_BYTES = 128
_MAX_KEY_BYTES = 512
_NAMESPACE = re.compile(r"^[a-z0-9][a-z0-9._:-]*$")
_REJECTED_CODES = frozenset(
    {
        "idempotency_invalid_input",
        "idempotency_invalid_key",
        "idempotency_invalid_namespace",
        "idempotency_invalid_result",
        "idempotency_key_mismatch",
        "idempotency_nested",
        "idempotency_input_too_large",
        "idempotency_private_table",
        "idempotency_result_too_large",
    }
)
_TABLE_IN_FIRST_ARGUMENT = frozenset(
    value
    for name in (
        "SQLITE_ANALYZE",
        "SQLITE_CREATE_TABLE",
        "SQLITE_CREATE_TEMP_TABLE",
        "SQLITE_CREATE_VTABLE",
        "SQLITE_DELETE",
        "SQLITE_DROP_TABLE",
        "SQLITE_DROP_TEMP_TABLE",
        "SQLITE_DROP_VTABLE",
        "SQLITE_INSERT",
        "SQLITE_READ",
        "SQLITE_UPDATE",
    )
    if isinstance((value := getattr(sqlite3, name, None)), int)
)
_TABLE_IN_SECOND_ARGUMENT = frozenset(
    value
    for name in (
        "SQLITE_ALTER_TABLE",
        "SQLITE_CREATE_INDEX",
        "SQLITE_CREATE_TEMP_INDEX",
        "SQLITE_CREATE_TEMP_TRIGGER",
        "SQLITE_CREATE_TRIGGER",
        "SQLITE_DROP_INDEX",
        "SQLITE_DROP_TEMP_INDEX",
        "SQLITE_DROP_TEMP_TRIGGER",
        "SQLITE_DROP_TRIGGER",
    )
    if isinstance((value := getattr(sqlite3, name, None)), int)
)
_ALWAYS_FORBIDDEN_ACTIONS = frozenset(
    value
    for name in (
        "SQLITE_ATTACH",
        "SQLITE_DETACH",
        "SQLITE_PRAGMA",
        "SQLITE_SAVEPOINT",
        "SQLITE_TRANSACTION",
    )
    if isinstance((value := getattr(sqlite3, name, None)), int)
)
_REINDEX_ACTION = getattr(sqlite3, "SQLITE_REINDEX", -1)


class IdempotencyError(RuntimeError):
    """A structured failure from the durable idempotency boundary."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        trace_id: str | None = None,
        recovery_hint: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        self.trace_id = trace_id or uuid.uuid4().hex
        self.recovery_hint = recovery_hint
        self.details = MappingProxyType(
            {"recoveryHint": recovery_hint, **dict(details or {})}
        )
        super().__init__(f"{message} [traceId={self.trace_id}]")


@dataclass(frozen=True, slots=True)
class IdempotencyResult:
    """The durable JSON value and whether this call replayed it."""

    value: Any
    replayed: bool
    operation_id: str
    original_operation_id: str


class IdempotencyTransaction:
    """The SQLite transaction owned by one first-time idempotent mutation."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._open = True
        self._denied_private_table = False
        private_indexes = connection.execute(
            f'PRAGMA index_list("{_TABLE}")'
        ).fetchall()
        self._private_reindex_targets = frozenset(
            {_TABLE.casefold(), *(str(row[1]).casefold() for row in private_indexes)}
        )
        connection.set_authorizer(self._authorize)

    def execute(
        self,
        sql: str,
        params: Mapping[str, Any] | Sequence[Any] = (),
    ) -> sqlite3.Cursor:
        """Execute one business statement inside the owned transaction."""

        self._require_open()
        return self._connection.execute(sql, params)

    def executemany(
        self,
        sql: str,
        params: Sequence[Mapping[str, Any] | Sequence[Any]],
    ) -> sqlite3.Cursor:
        """Execute one parameterized business statement for several rows."""

        self._require_open()
        return self._connection.executemany(sql, params)

    def query_one(
        self,
        sql: str,
        params: Mapping[str, Any] | Sequence[Any] = (),
    ) -> sqlite3.Row | None:
        """Return one row from a business query in the owned transaction."""

        cursor = self.execute(sql, params)
        try:
            return cursor.fetchone()
        finally:
            cursor.close()

    def query_all(
        self,
        sql: str,
        params: Mapping[str, Any] | Sequence[Any] = (),
    ) -> list[sqlite3.Row]:
        """Return all rows from a business query in the owned transaction."""

        cursor = self.execute(sql, params)
        try:
            return cursor.fetchall()
        finally:
            cursor.close()

    def _finish(self) -> None:
        if self._open:
            self._open = False
            self._connection.set_authorizer(None)

    def _require_open(self) -> None:
        if not self._open:
            raise IdempotencyError(
                "idempotency_transaction_closed",
                "The idempotency transaction is no longer active.",
                recovery_hint="Use the transaction only inside the run_once callback.",
            )

    def _authorize(
        self,
        action: int,
        argument_one: str | None,
        argument_two: str | None,
        _database_name: str | None,
        _trigger_name: str | None,
    ) -> int:
        table_name = (
            argument_one
            if action in _TABLE_IN_FIRST_ARGUMENT
            else argument_two
            if action in _TABLE_IN_SECOND_ARGUMENT
            else None
        )
        if (
            isinstance(table_name, str) and table_name.casefold() == _TABLE.casefold()
        ) or (
            action == _REINDEX_ACTION
            and isinstance(argument_one, str)
            and argument_one.casefold() in self._private_reindex_targets
        ):
            self._denied_private_table = True
            return sqlite3.SQLITE_DENY
        return (
            sqlite3.SQLITE_DENY
            if action in _ALWAYS_FORBIDDEN_ACTIONS
            else sqlite3.SQLITE_OK
        )

    def _policy_error(self) -> IdempotencyError | None:
        if not self._denied_private_table:
            return None
        return IdempotencyError(
            "idempotency_private_table",
            "Business mutation SQL cannot access the private idempotency receipt table.",
            recovery_hint="Read and write only business tables inside the run_once callback.",
        )


_audit_fields: contextvars.ContextVar[Mapping[str, Any]] = contextvars.ContextVar(
    "lite_mcp_idempotency_audit_fields", default=MappingProxyType({})
)
_active_stores: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "lite_mcp_idempotency_active_stores", default=()
)


@contextmanager
def _audit_context(**fields: Any):
    inherited = dict(_audit_fields.get())
    inherited.update({key: value for key, value in fields.items() if value is not None})
    token = _audit_fields.set(MappingProxyType(inherited))
    try:
        yield
    finally:
        _audit_fields.reset(token)


class IdempotencyStore:
    """Run explicitly keyed business callbacks exactly once after a success."""

    def __init__(
        self,
        database_path: Path | str,
        *,
        input_limit_bytes: int = _DEFAULT_INPUT_LIMIT_BYTES,
        result_limit_bytes: int = _DEFAULT_RESULT_LIMIT_BYTES,
        audit_sink: Any | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.database_path = Path(database_path).expanduser().resolve(strict=False)
        self.input_limit_bytes = _positive_limit("input_limit_bytes", input_limit_bytes)
        self.result_limit_bytes = _bounded_limit(
            "result_limit_bytes",
            result_limit_bytes,
            maximum=_DURABLE_RESULT_LIMIT_BYTES,
        )
        self.audit_sink = audit_sink
        self.logger = logger or _LOGGER

    def run_once(
        self,
        namespace: str,
        key: str,
        input_value: Any,
        mutation: Callable[[IdempotencyTransaction], Any],
    ) -> IdempotencyResult:
        """Run ``mutation`` once and durably replay its JSON result on retries."""

        namespace = _validate_namespace(namespace)
        key = _validate_key(key)
        if not callable(mutation):
            raise TypeError("mutation must be callable")
        input_json = _encode_json(
            input_value,
            limit=self.input_limit_bytes,
            invalid_code="idempotency_invalid_input",
            oversized_code="idempotency_input_too_large",
            value_name="idempotency input",
        )
        input_sha256 = hashlib.sha256(input_json).hexdigest()
        active = _active_stores.get()
        store_identity = str(self.database_path)
        if store_identity in active:
            raise IdempotencyError(
                "idempotency_nested",
                "Idempotent mutations cannot be nested on the same store.",
                recovery_hint="Keep the complete business mutation inside one run_once callback.",
            )

        context = dict(_audit_fields.get())
        operation_id = str(context.get("operationId") or uuid.uuid4().hex)
        transaction_id = uuid.uuid4().hex
        started_at = time.monotonic()
        audit_payload = {
            "namespace": namespace,
            "key": key,
            "inputSha256": input_sha256,
        }
        self._emit(
            "idempotency_started",
            payload=audit_payload,
            operationId=operation_id,
            transactionId=transaction_id,
        )
        token = _active_stores.set((*active, store_identity))
        try:
            result = self._run(
                namespace=namespace,
                key=key,
                input_sha256=input_sha256,
                mutation=mutation,
                operation_id=operation_id,
            )
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            retryable = (
                error.retryable if isinstance(error, IdempotencyError) else False
            )
            outcome = (
                "rejected"
                if isinstance(error, IdempotencyError) and error.code in _REJECTED_CODES
                else "failed"
            )
            self._emit(
                "idempotency_completed",
                level="WARNING" if outcome == "rejected" else "ERROR",
                payload=audit_payload,
                operationId=operation_id,
                transactionId=transaction_id,
                outcome=outcome,
                retryable=retryable,
                durationMs=_duration_ms(started_at),
                error=_error_payload(error),
                errorTraceId=(
                    error.trace_id if isinstance(error, IdempotencyError) else None
                ),
            )
            raise
        finally:
            _active_stores.reset(token)

        self._emit(
            "idempotency_completed",
            payload={**audit_payload, "replayed": result.replayed},
            operationId=operation_id,
            transactionId=transaction_id,
            outcome="success",
            durationMs=_duration_ms(started_at),
        )
        return result

    def _run(
        self,
        *,
        namespace: str,
        key: str,
        input_sha256: str,
        mutation: Callable[[IdempotencyTransaction], Any],
        operation_id: str,
    ) -> IdempotencyResult:
        connection: sqlite3.Connection | None = None
        transaction: IdempotencyTransaction | None = None
        try:
            connection = self._connect()
            existing = self._find(connection, namespace, key)
            if existing is not None:
                return self._replay(existing, input_sha256, operation_id)

            connection.execute("BEGIN IMMEDIATE")
            existing = self._find(connection, namespace, key)
            if existing is not None:
                connection.commit()
                return self._replay(existing, input_sha256, operation_id)

            transaction = IdempotencyTransaction(connection)
            value = mutation(transaction)
            transaction._finish()
            transaction = None
            result_json = _encode_json(
                value,
                limit=self.result_limit_bytes,
                invalid_code="idempotency_invalid_result",
                oversized_code="idempotency_result_too_large",
                value_name="idempotency result",
            )
            result_sha256 = hashlib.sha256(result_json).hexdigest()
            connection.execute(
                f"""
                INSERT INTO {_TABLE} (
                    namespace, business_key, input_sha256, result_json,
                    result_sha256, result_bytes, original_operation_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    namespace,
                    key,
                    input_sha256,
                    result_json,
                    result_sha256,
                    len(result_json),
                    operation_id,
                ),
            )
            connection.commit()
            return IdempotencyResult(
                value=json.loads(result_json),
                replayed=False,
                operation_id=operation_id,
                original_operation_id=operation_id,
            )
        except IdempotencyError:
            if transaction is not None:
                transaction._finish()
                transaction = None
            if connection is not None:
                _rollback_quietly(connection, self.logger)
            raise
        except sqlite3.Error as error:
            policy_error = (
                transaction._policy_error() if transaction is not None else None
            )
            if transaction is not None:
                transaction._finish()
                transaction = None
            if connection is not None:
                _rollback_quietly(connection, self.logger)
            if policy_error is not None:
                raise policy_error from error
            self._log_database_failure(error)
            raise _sqlite_error(error) from error
        except BaseException:
            if transaction is not None:
                transaction._finish()
                transaction = None
            if connection is not None:
                _rollback_quietly(connection, self.logger)
            raise
        finally:
            if transaction is not None:
                transaction._finish()
            if connection is not None:
                connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.enable_load_extension(False)
            if hasattr(connection, "setconfig") and hasattr(
                sqlite3, "SQLITE_DBCONFIG_DEFENSIVE"
            ):
                connection.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
            _configure_wal(connection)
            return connection
        except BaseException:
            connection.close()
            raise

    @staticmethod
    def _find(
        connection: sqlite3.Connection, namespace: str, key: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            f"""
            SELECT input_sha256, result_json, result_sha256, result_bytes,
                   original_operation_id
            FROM {_TABLE}
            WHERE namespace = ? AND business_key = ?
            """,
            (namespace, key),
        ).fetchone()

    @staticmethod
    def _replay(
        row: sqlite3.Row, input_sha256: str, operation_id: str
    ) -> IdempotencyResult:
        if row["input_sha256"] != input_sha256:
            raise IdempotencyError(
                "idempotency_key_mismatch",
                "The idempotency key was already used with different input.",
                recovery_hint="Reuse a key only for the exact same business request, or choose a new key.",
                details={
                    "originalInputSha256": row["input_sha256"],
                    "receivedInputSha256": input_sha256,
                },
            )
        payload = bytes(row["result_json"])
        if (
            len(payload) != row["result_bytes"]
            or hashlib.sha256(payload).hexdigest() != row["result_sha256"]
        ):
            raise IdempotencyError(
                "idempotency_record_corrupt",
                "The stored idempotency result failed its integrity check.",
                recovery_hint="Restore or repair the database before retrying this key.",
            )
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise IdempotencyError(
                "idempotency_record_corrupt",
                "The stored idempotency result is not valid JSON.",
                recovery_hint="Restore or repair the database before retrying this key.",
            ) from error
        return IdempotencyResult(
            value=value,
            replayed=True,
            operation_id=operation_id,
            original_operation_id=str(row["original_operation_id"]),
        )

    def _emit(self, event: str, *, level: str = "INFO", **fields: Any) -> None:
        if self.audit_sink is None:
            return
        metadata = dict(_audit_fields.get())
        metadata.update(fields)
        self.audit_sink.emit("audit", event, level=level, **metadata)

    def _log_database_failure(self, error: sqlite3.Error) -> None:
        trace = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        self.logger.error(
            "idempotency database failure exception_type=%s\n%s",
            type(error).__name__,
            trace,
        )


_configuration_lock = threading.RLock()
_configured_store: IdempotencyStore | None = None


def _configure(database_path: Path | str, *, audit_sink: Any | None = None) -> None:
    """Configure the process-global helper before tools can be invoked."""

    global _configured_store
    with _configuration_lock:
        _configured_store = IdempotencyStore(database_path, audit_sink=audit_sink)


def run_once(
    namespace: str,
    key: str,
    input_value: Any,
    mutation: Callable[[IdempotencyTransaction], Any],
) -> IdempotencyResult:
    """Run a mutation through the process-configured durable store."""

    with _configuration_lock:
        store = _configured_store
    if store is None:
        raise RuntimeError("lite-mcp idempotency is not configured")
    return store.run_once(namespace, key, input_value, mutation)


def _positive_limit(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _bounded_limit(name: str, value: int, *, maximum: int) -> int:
    value = _positive_limit(name, value)
    if value > maximum:
        raise ValueError(
            f"{name} must be no greater than the {maximum}-byte durable schema limit"
        )
    return value


def _configure_wal(connection: sqlite3.Connection) -> None:
    deadline = time.monotonic() + (_BUSY_TIMEOUT_MS / 1000)
    while True:
        try:
            row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
            mode = str(row[0]).casefold() if row is not None else ""
            if mode != "wal":
                raise sqlite3.OperationalError(
                    f"SQLite did not enter WAL journal mode (reported {mode or 'no mode'})"
                )
            return
        except sqlite3.OperationalError as error:
            remaining = deadline - time.monotonic()
            if not _is_busy_error(error) or remaining <= 0:
                raise
            time.sleep(min(0.01, remaining))


def _is_busy_error(error: sqlite3.Error) -> bool:
    text = str(error).casefold()
    return "locked" in text or "busy" in text


def _validate_namespace(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > _MAX_NAMESPACE_BYTES
        or _NAMESPACE.fullmatch(value) is None
    ):
        raise IdempotencyError(
            "idempotency_invalid_namespace",
            "The idempotency namespace is invalid.",
            recovery_hint=(
                "Use 1-128 bytes of lowercase ASCII letters, digits, '.', '_', ':', or '-'."
            ),
        )
    return value


def _validate_key(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > _MAX_KEY_BYTES
    ):
        raise IdempotencyError(
            "idempotency_invalid_key",
            "The idempotency business key is invalid.",
            recovery_hint="Use a non-empty, trimmed UTF-8 key no larger than 512 bytes.",
        )
    return value


def _encode_json(
    value: Any,
    *,
    limit: int,
    invalid_code: str,
    oversized_code: str,
    value_name: str,
) -> bytes:
    try:
        _validate_json_value(value)
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise IdempotencyError(
            invalid_code,
            f"The {value_name} is not canonical JSON.",
            recovery_hint="Use only JSON objects with string keys, arrays, and finite JSON scalar values.",
            details={"exceptionType": type(error).__name__},
        ) from error
    if len(encoded) > limit:
        raise IdempotencyError(
            oversized_code,
            f"The encoded {value_name} exceeds the {limit}-byte limit.",
            recovery_hint=f"Reduce the {value_name} below {limit} encoded bytes.",
            details={"encodedBytes": len(encoded), "limitBytes": limit},
        )
    return encoded


def _validate_json_value(value: Any, *, seen: set[int] | None = None) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return
    identities = seen if seen is not None else set()
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in identities:
            raise ValueError("cyclic JSON object")
        identities.add(identity)
        try:
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError("JSON object keys must be strings")
                _validate_json_value(item, seen=identities)
        finally:
            identities.remove(identity)
        return
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in identities:
            raise ValueError("cyclic JSON array")
        identities.add(identity)
        try:
            for item in value:
                _validate_json_value(item, seen=identities)
        finally:
            identities.remove(identity)
        return
    raise TypeError(f"unsupported JSON type: {type(value).__name__}")


def _sqlite_error(error: sqlite3.Error) -> IdempotencyError:
    text = str(error).casefold()
    if _is_busy_error(error):
        return IdempotencyError(
            "idempotency_database_busy",
            "The database is busy while claiming the idempotency key.",
            retryable=True,
            recovery_hint="Wait briefly for the active writer, then retry the same key and input.",
            details={"exceptionType": type(error).__name__},
        )
    if isinstance(error, sqlite3.IntegrityError):
        return IdempotencyError(
            "constraint_violation",
            "The idempotent business mutation violated a database constraint.",
            recovery_hint="Correct the business input, then use a new idempotency key.",
            details={"exceptionType": type(error).__name__},
        )
    if any(marker in text for marker in ("disk i/o error", "database or disk is full")):
        return IdempotencyError(
            "idempotency_database_io",
            "Database storage failed during the idempotent mutation.",
            retryable=True,
            recovery_hint="Restore storage availability, then retry the same key and input.",
            details={"exceptionType": type(error).__name__},
        )
    return IdempotencyError(
        "idempotency_database_error",
        "The database could not complete the idempotent mutation.",
        recovery_hint="Inspect the server log using the trace ID before retrying.",
        details={"exceptionType": type(error).__name__},
    )


def _rollback_quietly(connection: sqlite3.Connection, logger: logging.Logger) -> None:
    try:
        connection.rollback()
    except sqlite3.Error:
        logger.exception("idempotency rollback failed")


def _error_payload(error: BaseException) -> dict[str, Any]:
    if isinstance(error, IdempotencyError):
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
    return {
        "type": type(error).__name__,
        "message": str(error),
        "traceId": None,
        "traceback": "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        ),
    }


def _duration_ms(started_at: float) -> float:
    return round((time.monotonic() - started_at) * 1000, 3)
