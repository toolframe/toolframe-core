"""Durable, explicit idempotency using the selected database provider."""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import math
import re
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Mapping
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

_LOGGER = logging.getLogger("toolframe.idempotency")
_TABLE = "_toolframe_idempotency"
_RECEIPT_SCHEMA = """
CREATE TABLE _toolframe_idempotency (
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
    """Bounded business operations inside one provider-owned idempotent write."""

    def __init__(self, provider, connection):
        self._provider = provider
        self._connection = connection
        self._open = True
        self._failed = False

    def _call(self, method, *args, **kwargs):
        self._require_open()
        try:
            return method(*args, **kwargs, private=True)
        except Exception:
            self._failed = True
            raise

    def execute(self, sql, params=()):
        result, _ = self._call(
            self._provider._execute_on_connection, self._connection, sql, params
        )
        return result

    def insert(self, table, values, *, id_column="id"):
        from toolframe_database import _insert

        self._require_open()
        try:
            return _insert(self, table, values, id_column=id_column)
        except Exception:
            self._failed = True
            raise

    def query(self, sql, params=(), *, max_rows=None, max_bytes=None):
        return self._call(
            self._provider._query_on_connection,
            self._connection,
            sql,
            params,
            max_rows=max_rows,
            max_bytes=max_bytes,
        )

    def query_one(self, sql, params=()):
        rows = self.query_all(sql, params)
        return rows[0] if rows else None

    def query_all(self, sql, params=()):
        return self.query(sql, params).to_dict(orient="records")

    def executemany(self, sql, params):
        from toolframe_database import ExecuteResult

        count, last = 0, None
        for values in params:
            result = self.execute(sql, values)
            count += result.affected_rows
            last = result.last_insert_rowid
        return ExecuteResult(count, last)

    def _finish(self):
        self._open = False

    def _require_open(self):
        if not self._open or self._failed:
            raise IdempotencyError(
                "idempotency_transaction_closed"
                if not self._open
                else "idempotency_transaction_aborted",
                "The idempotency transaction cannot continue.",
                recovery_hint="Use the transaction only inside a successful run_once callback.",
            )


_audit_fields: contextvars.ContextVar[Mapping[str, Any]] = contextvars.ContextVar(
    "toolframe_idempotency_audit_fields", default=MappingProxyType({})
)
_active_stores: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "toolframe_idempotency_active_stores", default=()
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
        database_path: Path | str | None = None,
        *,
        provider: Any = None,
        input_limit_bytes: int = _DEFAULT_INPUT_LIMIT_BYTES,
        result_limit_bytes: int = _DEFAULT_RESULT_LIMIT_BYTES,
        audit_sink: Any | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if provider is None:
            from toolframe_database import SQLiteProvider

            provider = SQLiteProvider(
                database_path,
                allow_schema_changes=False,
                protected_table_prefixes=("_toolframe_", "sqlite_"),
                logger=logger,
                audit_sink=audit_sink,
            )
        self.provider = provider
        self.database_path = getattr(provider, "path", None)
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
        store_identity = self.provider.identity
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

    def _provider_call(self, call, *args, **kwargs):
        try:
            return call(*args, **kwargs)
        except Exception as error:  # noqa: BLE001 - normalize provider failures.
            self.provider._raise_public(error, "idempotency storage operation")

    def _find(self, connection, namespace, key):
        cursor = self._provider_call(
            connection.execute,
            f"SELECT input_sha256, result_json, result_sha256, result_bytes, "
            f"original_operation_id FROM {_TABLE} "
            "WHERE namespace = :namespace AND business_key = :key",
            {"namespace": namespace, "key": key},
        )
        try:
            row = cursor.fetchone()
            return (
                dict(zip((column[0] for column in cursor.description), row))
                if row is not None
                else None
            )
        finally:
            cursor.close()

    def _run(self, *, namespace, key, input_sha256, mutation, operation_id):
        from toolframe_database import DashboardDatabaseError

        transaction = None
        try:
            with self.provider.connection() as connection:
                try:
                    self._provider_call(
                        self.provider.begin_locked,
                        connection,
                        f"idempotency:{len(namespace)}:{namespace}:{key}",
                    )
                    existing = self._find(connection, namespace, key)
                    if existing is not None:
                        result = self._replay(existing, input_sha256, operation_id)
                        self._provider_call(connection.commit)
                        return result
                    transaction = IdempotencyTransaction(self.provider, connection)
                    value = mutation(transaction)
                    transaction._require_open()
                    transaction._finish()
                    result_json = _encode_json(
                        value,
                        limit=self.result_limit_bytes,
                        invalid_code="idempotency_invalid_result",
                        oversized_code="idempotency_result_too_large",
                        value_name="idempotency result",
                    )
                    self._provider_call(
                        connection.execute,
                        f"INSERT INTO {_TABLE} (namespace, business_key, input_sha256, "
                        "result_json, result_sha256, result_bytes, original_operation_id) "
                        "VALUES (:namespace, :key, :input, :result, :hash, :size, :operation)",
                        {
                            "namespace": namespace,
                            "key": key,
                            "input": input_sha256,
                            "result": result_json,
                            "hash": hashlib.sha256(result_json).hexdigest(),
                            "size": len(result_json),
                            "operation": operation_id,
                        },
                    ).close()
                    self._provider_call(connection.commit)
                    return IdempotencyResult(
                        json.loads(result_json), False, operation_id, operation_id
                    )
                except BaseException:
                    _rollback_quietly(connection, self.logger)
                    raise
                finally:
                    if transaction is not None:
                        transaction._finish()
        except DashboardDatabaseError as error:
            code = {
                "database_busy": "idempotency_database_busy",
                "database_io": "idempotency_database_io",
                "protected_table_forbidden": "idempotency_private_table",
                "constraint_violation": "constraint_violation",
            }.get(error.code, "idempotency_database_error")
            raise IdempotencyError(
                code,
                error.message,
                retryable=error.retryable,
                trace_id=error.trace_id,
                recovery_hint=error.recovery_hint,
                details={"databaseCode": error.code},
            ) from None

    @staticmethod
    def _replay(
        row: Mapping[str, Any], input_sha256: str, operation_id: str
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


_configuration_lock = threading.RLock()
_configured_store: IdempotencyStore | None = None


def _configure(
    database_path: Path | str | None = None,
    *,
    provider=None,
    audit_sink: Any | None = None,
) -> None:
    """Configure the process-global helper before tools can be invoked."""

    global _configured_store
    with _configuration_lock:
        _configured_store = IdempotencyStore(
            database_path, provider=provider, audit_sink=audit_sink
        )


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
        raise RuntimeError("toolframe idempotency is not configured")
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


def _rollback_quietly(connection: Any, logger: logging.Logger) -> None:
    try:
        connection.rollback()
    except Exception as error:  # noqa: BLE001 - rollback errors must not expose a DSN.
        logger.error(
            "idempotency rollback failed exception_type=%s", type(error).__name__
        )


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
