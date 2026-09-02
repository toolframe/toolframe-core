"""Fail-closed structured JSONL writers for lite-mcp.

This module deliberately does not use :mod:`logging`: audit records are a
contract, rather than best-effort diagnostic messages.  Callers must treat a
``StructuredLogError`` as fatal.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Self

Channel = Literal["audit", "mcp"]
Clock = Callable[[], datetime]

_MANAGED_NAME = re.compile(r"^(audit|mcp)-(\d{4}-\d{2}-\d{2})\.jsonl$")
_PREVIEW_MARKER = "\n... <middle truncated> ...\n"
_COMMON_FIELDS = (
    "schemaVersion",
    "timestamp",
    "sequence",
    "channel",
    "level",
    "event",
    "instanceId",
    "operationId",
    "protocolRequestId",
    "transactionId",
    "dashboardSessionId",
    "sessionId",
    "sourceIp",
    "sourcePort",
    "client",
    "method",
    "tool",
    "widget",
    "payload",
    "outcome",
    "retryable",
    "durationMs",
    "error",
    "errorTraceId",
    "otelTraceId",
    "userAgent",
    "contentType",
    "contentLength",
    "protocolVersion",
    "direction",
    "httpMethod",
    "path",
    "statusCode",
    "requestId",
)
_COMMON_FIELD_SET = frozenset(_COMMON_FIELDS)


class StructuredLogError(RuntimeError):
    """A fatal structured-log setup, serialization, or write failure."""


@dataclass(frozen=True, slots=True)
class StructuredLogSettings:
    """Settings shared by the audit and MCP writers."""

    logs_dir: Path
    retention_days: int = 14
    max_total_mib: int = 256
    payload_limit_bytes: int = 16 * 1024
    traceback_limit_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        object.__setattr__(self, "logs_dir", Path(self.logs_dir))
        if self.retention_days < 1:
            raise ValueError("retention_days must be at least 1")
        if self.max_total_mib < 1:
            raise ValueError("max_total_mib must be at least 1")
        if self.payload_limit_bytes < 1:
            raise ValueError("payload_limit_bytes must be at least 1")
        if self.traceback_limit_bytes < 1:
            raise ValueError("traceback_limit_bytes must be at least 1")

    @property
    def max_total_bytes(self) -> int:
        return self.max_total_mib * 1024 * 1024


class StructuredLogs:
    """Two synchronous, process-exclusive UTC daily JSONL writers."""

    def __init__(
        self,
        settings: StructuredLogSettings,
        *,
        clock: Clock | None = None,
        instance_id: str | None = None,
    ) -> None:
        self.settings = settings
        self._clock = clock or (lambda: datetime.now(UTC))
        self._instance_id = instance_id or str(uuid.uuid4())
        self._mutex = threading.RLock()
        self._sequence = 0
        self._closed = False
        self._handles: dict[Channel, tuple[date, Any]] = {}
        self._lock_handle: Any | None = None
        try:
            self.settings.logs_dir.mkdir(parents=True, exist_ok=True)
            if not self.settings.logs_dir.is_dir():
                raise StructuredLogError(
                    f"logs path is not a directory: {self.settings.logs_dir}"
                )
            self._acquire_process_lock()
            managed = self._scan_managed()
            recoveries = self._recover_partial_lines(managed)
            now = self._now()
            deletions = self._prepare_capacity(0, now.date(), now)
            for recovery in recoveries:
                self.emit("audit", "log_tail_recovered", payload=recovery)
            for removed_date in deletions:
                self.emit(
                    "audit",
                    "log_retention_deleted",
                    payload={"date": removed_date, "reason": "startup_cleanup"},
                )
        except StructuredLogError:
            self._close_resources()
            raise
        except BaseException as error:
            self._close_resources()
            raise StructuredLogError(
                f"could not initialize structured logs at {self.settings.logs_dir}: {error}"
            ) from error

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def logs_dir(self) -> Path:
        return self.settings.logs_dir

    def emit(
        self,
        channel: Channel,
        event: str,
        level: str = "INFO",
        payload: Any = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """Synchronously append and flush one event, returning its exact record."""

        if channel not in {"audit", "mcp"}:
            raise ValueError("channel must be 'audit' or 'mcp'")
        if not event:
            raise ValueError("event must not be empty")
        with self._mutex:
            if self._closed:
                raise StructuredLogError("structured logs are closed")
            try:
                timestamp = self._now()
                event_date = timestamp.date()
                normalized_payload = _bounded_value(
                    payload, self.settings.payload_limit_bytes
                )
                normalized_fields = {
                    key: self._normalize_field(key, value)
                    for key, value in fields.items()
                }
                while True:
                    record = self._record(
                        sequence=self._sequence + 1,
                        timestamp=timestamp,
                        channel=channel,
                        event=event,
                        level=level,
                        payload=normalized_payload,
                        fields=normalized_fields,
                    )
                    line = _json_line(record)
                    removed = self._prepare_capacity(len(line), event_date, timestamp)
                    if not removed:
                        break
                    for removed_date in removed:
                        self._write_retention_event(timestamp, removed_date)
                self._assert_capacity(len(line))
                self._append(channel, event_date, line)
                self._sequence += 1
                return record
            except StructuredLogError:
                raise
            except BaseException as error:
                raise StructuredLogError(
                    f"could not emit {channel} event {event!r}: {error}"
                ) from error

    def close(self) -> None:
        with self._mutex:
            if self._closed:
                return
            self._closed = True
            try:
                self._close_resources()
            except BaseException as error:
                raise StructuredLogError(
                    f"could not close structured logs: {error}"
                ) from error

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise StructuredLogError("structured log clock returned a naive datetime")
        return value.astimezone(UTC)

    def _normalize_field(self, key: str, value: Any) -> Any:
        normalized = _normalize(value)
        if key == "error" and isinstance(normalized, dict):
            traceback_value = normalized.get("traceback")
            if traceback_value is not None:
                normalized["traceback"] = _bounded_traceback(
                    traceback_value, self.settings.traceback_limit_bytes
                )
        return normalized

    def _record(
        self,
        *,
        sequence: int,
        timestamp: datetime,
        channel: Channel,
        event: str,
        level: str,
        payload: Any,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        protected = {
            "schemaVersion",
            "timestamp",
            "sequence",
            "channel",
            "event",
            "level",
            "instanceId",
            "payload",
        }
        conflict = protected.intersection(fields)
        if conflict:
            raise ValueError(
                "reserved structured log fields cannot be overridden: "
                + ", ".join(sorted(conflict))
            )
        unknown = set(fields).difference(_COMMON_FIELD_SET)
        if unknown:
            raise ValueError(
                "unknown structured log fields: " + ", ".join(sorted(unknown))
            )
        record = {key: None for key in _COMMON_FIELDS}
        record.update(fields)
        record.update(
            {
                "schemaVersion": 1,
                "timestamp": timestamp.isoformat(timespec="microseconds").replace(
                    "+00:00", "Z"
                ),
                "sequence": sequence,
                "channel": channel,
                "level": str(level).upper(),
                "event": event,
                "instanceId": self._instance_id,
                "payload": payload,
            }
        )
        return record

    def _acquire_process_lock(self) -> None:
        lock_path = self.settings.logs_dir / ".lite-mcp.lock"
        if lock_path.is_symlink() or (lock_path.exists() and not lock_path.is_file()):
            raise StructuredLogError(f"invalid structured log lock path: {lock_path}")
        try:
            handle = lock_path.open("a+", encoding="utf-8")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as error:
            try:
                handle.close()
            except (UnboundLocalError, OSError):
                pass
            raise StructuredLogError(
                f"logs directory is already in use: {self.settings.logs_dir}"
            ) from error
        self._lock_handle = handle

    def _scan_managed(self) -> dict[str, dict[Channel, Path]]:
        managed: dict[str, dict[Channel, Path]] = {}
        try:
            entries = list(self.settings.logs_dir.iterdir())
        except OSError as error:
            raise StructuredLogError(
                f"could not inspect logs directory {self.settings.logs_dir}: {error}"
            ) from error
        for path in entries:
            match = _MANAGED_NAME.fullmatch(path.name)
            if match is None:
                continue
            try:
                mode = path.lstat().st_mode
            except OSError as error:
                raise StructuredLogError(
                    f"could not inspect managed log {path}: {error}"
                ) from error
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise StructuredLogError(
                    f"managed log path must be a direct regular file: {path}"
                )
            try:
                parsed = date.fromisoformat(match.group(2))
            except ValueError as error:
                raise StructuredLogError(f"invalid managed log date: {path}") from error
            if parsed.isoformat() != match.group(2):
                raise StructuredLogError(f"invalid managed log date: {path}")
            channel = match.group(1)
            managed.setdefault(match.group(2), {})[channel] = path  # type: ignore[index]
        return managed

    def _recover_partial_lines(
        self, managed: dict[str, dict[Channel, Path]]
    ) -> list[dict[str, Any]]:
        recovered: list[dict[str, Any]] = []
        for files in managed.values():
            for path in files.values():
                try:
                    size = path.stat().st_size
                    if size == 0:
                        continue
                    with path.open("rb+") as handle:
                        handle.seek(-1, os.SEEK_END)
                        if handle.read(1) == b"\n":
                            continue
                        retained = _last_complete_line_end(handle, size)
                        handle.truncate(retained)
                    recovered.append(
                        {
                            "path": path.name,
                            "removedBytes": size - retained,
                            "retainedBytes": retained,
                        }
                    )
                except OSError as error:
                    raise StructuredLogError(
                        f"could not recover partial managed log {path}: {error}"
                    ) from error
        return recovered

    def _apply_age_retention(
        self, managed: dict[str, dict[Channel, Path]], current: date
    ) -> list[str]:
        cutoff = current - timedelta(days=self.settings.retention_days - 1)
        removed: list[str] = []
        for date_text in sorted(managed):
            if date.fromisoformat(date_text) < cutoff:
                self._delete_bucket(date_text, managed[date_text])
                removed.append(date_text)
        return removed

    def _prepare_capacity(
        self, incoming_bytes: int, current: date, timestamp: datetime
    ) -> list[str]:
        managed = self._scan_managed()
        removed = self._apply_age_retention(managed, current)
        while True:
            retention_bytes = sum(
                len(
                    self._retention_line(
                        timestamp, date_text, self._sequence + index + 1
                    )
                )
                for index, date_text in enumerate(removed)
            )
            # A later sequence can gain a handful of digits. Reserve one byte
            # per possible decimal place rather than an arbitrary record-sized
            # cushion.
            required = incoming_bytes + retention_bytes + (32 if removed else 0)
            additionally_removed = self._make_room(required, current)
            if not additionally_removed:
                break
            removed.extend(additionally_removed)
        return removed

    def _make_room(self, incoming_bytes: int, current: date) -> list[str]:
        managed = self._scan_managed()
        total = sum(
            path.stat().st_size for files in managed.values() for path in files.values()
        )
        removed: list[str] = []
        for date_text in sorted(managed):
            if total + incoming_bytes <= self.settings.max_total_bytes:
                break
            # Never delete the active bucket or a future-dated bucket. A
            # future file may reflect clock correction and is not the oldest
            # eligible completed UTC day.
            if date_text >= current.isoformat():
                continue
            bucket_size = sum(
                path.stat().st_size for path in managed[date_text].values()
            )
            self._delete_bucket(date_text, managed[date_text])
            total -= bucket_size
            removed.append(date_text)
        if total + incoming_bytes > self.settings.max_total_bytes:
            raise StructuredLogError(
                "managed structured logs reached the configured total size cap"
            )
        return removed

    def _assert_capacity(self, incoming_bytes: int) -> None:
        managed = self._scan_managed()
        total = sum(
            path.stat().st_size for files in managed.values() for path in files.values()
        )
        if total + incoming_bytes > self.settings.max_total_bytes:
            raise StructuredLogError(
                "managed structured logs reached the configured total size cap"
            )

    def _delete_bucket(self, date_text: str, files: Mapping[Channel, Path]) -> None:
        self._close_bucket(date.fromisoformat(date_text))
        for path in files.values():
            try:
                path.unlink()
            except OSError as error:
                raise StructuredLogError(
                    f"could not delete retained log {path}: {error}"
                ) from error

    def _write_retention_event(self, timestamp: datetime, removed_date: str) -> None:
        line = self._retention_line(timestamp, removed_date, self._sequence + 1)
        self._assert_capacity(len(line))
        self._append("audit", timestamp.date(), line)
        self._sequence += 1

    def _retention_line(
        self, timestamp: datetime, removed_date: str, sequence: int
    ) -> bytes:
        return _json_line(
            self._record(
                sequence=sequence,
                timestamp=timestamp,
                channel="audit",
                event="log_retention_deleted",
                level="INFO",
                payload={"date": removed_date, "reason": "retention_policy"},
                fields={},
            )
        )

    def _append(self, channel: Channel, event_date: date, line: bytes) -> None:
        handle = self._handle(channel, event_date)
        try:
            written = handle.write(line)
            if written != len(line):
                raise OSError("short structured log write")
            handle.flush()
        except BaseException as error:
            raise StructuredLogError(
                f"could not append {channel} log {handle.name}: {error}"
            ) from error

    def _handle(self, channel: Channel, event_date: date) -> Any:
        existing = self._handles.get(channel)
        if existing is not None and existing[0] == event_date:
            return existing[1]
        if existing is not None:
            self._close_one(existing[1])
            del self._handles[channel]
        path = self.settings.logs_dir / f"{channel}-{event_date.isoformat()}.jsonl"
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise StructuredLogError(
                f"managed log path must be a direct regular file: {path}"
            )
        try:
            handle = path.open("ab", buffering=0)
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                handle.close()
                raise StructuredLogError(
                    f"managed log path must be a regular file: {path}"
                )
        except StructuredLogError:
            raise
        except OSError as error:
            raise StructuredLogError(
                f"could not open managed log {path}: {error}"
            ) from error
        self._handles[channel] = (event_date, handle)
        return handle

    def _close_bucket(self, bucket: date) -> None:
        for channel, (event_date, handle) in list(self._handles.items()):
            if event_date == bucket:
                self._close_one(handle)
                del self._handles[channel]

    @staticmethod
    def _close_one(handle: Any) -> None:
        handle.flush()
        handle.close()

    def _close_resources(self) -> None:
        first_error: BaseException | None = None
        for _, handle in list(self._handles.values()):
            try:
                self._close_one(handle)
            except (OSError, ValueError) as error:
                first_error = first_error or error
        self._handles.clear()
        if self._lock_handle is not None:
            try:
                fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
                self._lock_handle.close()
            except (OSError, ValueError) as error:
                first_error = first_error or error
            self._lock_handle = None
        if first_error is not None:
            raise first_error


def _normalize(value: Any, *, _seen: set[int] | None = None) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite floats are not JSON-compatible")
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return {
            "encoding": "base64",
            "data": base64.b64encode(raw).decode("ascii"),
        }
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (Path, uuid.UUID)):
        return str(value)
    seen = _seen if _seen is not None else set()
    identity = id(value)
    if identity in seen:
        raise ValueError("cyclic value is not JSON-compatible")
    seen.add(identity)
    try:
        if isinstance(value, Mapping):
            return {
                str(key): _normalize(item, _seen=seen) for key, item in value.items()
            }
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray, memoryview)
        ):
            return [_normalize(item, _seen=seen) for item in value]
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return _normalize(model_dump(mode="json"), _seen=seen)
        raise TypeError(f"unsupported JSON value: {type(value).__name__}")
    finally:
        seen.remove(identity)


def _bounded_value(value: Any, limit: int) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        normalized_bytes = _normalize(raw)
        encoded = json.dumps(
            normalized_bytes,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) <= limit:
            return normalized_bytes
        base64_text = base64.b64encode(raw).decode("ascii")
        first_size = limit // 2
        last_size = limit - first_size
        return {
            "encoding": "base64",
            "truncated": True,
            "originalBytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "preview": (
                base64_text[:first_size] + _PREVIEW_MARKER + base64_text[-last_size:]
            ),
        }
    normalized = _normalize(value)
    encoded = json.dumps(
        normalized, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) <= limit:
        return normalized
    first_size = limit // 2
    last_size = limit - first_size
    preview = (
        encoded[:first_size].decode("utf-8", errors="replace")
        + _PREVIEW_MARKER
        + encoded[-last_size:].decode("utf-8", errors="replace")
    )
    return {
        "truncated": True,
        "originalBytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "preview": preview,
    }


def _bounded_traceback(value: Any, limit: int) -> Any:
    if not isinstance(value, str):
        return _bounded_value(value, limit)
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    first_size = limit // 2
    last_size = limit - first_size
    return {
        "truncated": True,
        "originalBytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "preview": (
            encoded[:first_size].decode("utf-8", errors="replace")
            + _PREVIEW_MARKER
            + encoded[-last_size:].decode("utf-8", errors="replace")
        ),
    }


def _json_line(record: dict[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                record,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise StructuredLogError(
            f"could not serialize structured log record: {error}"
        ) from error


def _last_complete_line_end(handle: Any, size: int) -> int:
    """Find the last newline without loading a potentially large log file."""

    position = size
    chunk_size = 64 * 1024
    while position > 0:
        start = max(0, position - chunk_size)
        handle.seek(start)
        chunk = handle.read(position - start)
        newline = chunk.rfind(b"\n")
        if newline >= 0:
            return start + newline + 1
        position = start
    return 0
