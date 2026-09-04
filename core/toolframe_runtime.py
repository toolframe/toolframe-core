"""Private runtime support for the toolframe FastAPI/FastMCP server."""

from __future__ import annotations

import base64
import errno
import hashlib
import importlib.util
import inspect
import json
import logging
import math
import os
import re
import signal
import sqlite3
import stat
import sys
import threading
import time
import traceback
import types
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import toolframe_db as database_sdk
import toolframe_idempotency as idempotency
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastmcp import FastMCP
from fastmcp.decorators import get_fastmcp_meta
from fastmcp.server.middleware import Middleware
from fastmcp.tools import Tool, ToolResult
from fastmcp.tools.function_tool import ToolMeta
from fastmcp.utilities.lifespan import combine_lifespans
from toolframe_artifacts import (
    DEFAULT_MAX_ARTIFACT_BYTES,
    ArtifactError,
    configure_artifact_store,
)
from toolframe_logging import StructuredLogError, StructuredLogs, StructuredLogSettings
from starlette.routing import Route

LOGGER = logging.getLogger("toolframe.runtime")
READY_SENTINEL = "TOOLFRAME_READY"
FATAL_SENTINEL = "TOOLFRAME_FATAL"
DATABASE_PREVIEW_ROWS = 25
STREAMLIT_VERSION = "1.61.0"
STREAMLIT_RESOURCE_LIMIT_MIB = 25
LLMS_MAX_BYTES = 256 * 1024
_LLMS_DOCUMENT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*\.md\Z")
_LLMS_LOCAL_LINK = re.compile(
    r"(?:^|(?<=[(<\s\"']))/llms/([^\s<>'\"()\]]+)", re.MULTILINE
)
_STANDARD_HTTP_METHODS = {
    "GET",
    "HEAD",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "OPTIONS",
    "TRACE",
}


class LlmsValidationError(ValueError, RuntimeError):
    """A safe, source-specific validation failure for the agent context tree."""

    def __init__(self, source: Path, reason: str) -> None:
        self.source = Path(source)
        self.reason = reason
        super().__init__(f"{self.source}: {reason}")


def validate_llms_source(index_file: Path, docs_dir: Path) -> list[str]:
    """Validate an llms.txt index and return its safe, reachable detail files.

    The validator intentionally returns filenames (including ``.md``) rather
    than paths.  Callers can use that allowlist to ensure later requests never
    turn an arbitrary path into a file server.
    """

    documents, _ = _validated_llms_source(Path(index_file), Path(docs_dir))
    return documents


def _validated_llms_source(index_file: Path, docs_dir: Path) -> tuple[list[str], bytes]:
    index_bytes = _read_llms_utf8(index_file, source_label="llms index")
    index_text = index_bytes.decode("utf-8")
    _validate_llms_index_format(index_file, index_text)

    _validate_llms_directory(docs_dir)
    index_location = index_file.absolute()
    documents: list[str] = []
    for entry in _scan_llms_directory(docs_dir):
        entry_path = Path(entry.path)
        if entry_path.absolute() == index_location:
            continue
        if entry.is_symlink():
            raise LlmsValidationError(
                entry_path,
                "detail must be a regular, non-symlink file",
            )
        if not _LLMS_DOCUMENT_NAME.fullmatch(entry.name):
            raise LlmsValidationError(
                entry_path,
                "detail filename must match safe lowercase [a-z0-9][a-z0-9_-]*.md",
            )
        try:
            entry_stat = entry.stat(follow_symlinks=False)
        except OSError as error:
            raise LlmsValidationError(
                entry_path,
                f"could not inspect detail ({_os_error_reason(error)})",
            ) from error
        if not stat.S_ISREG(entry_stat.st_mode):
            raise LlmsValidationError(
                entry_path,
                "detail must be a regular, non-symlink file",
            )
        _read_llms_utf8(entry_path, source_label="llms detail")
        documents.append(entry.name)

    if not documents:
        raise LlmsValidationError(docs_dir, "at least one detail .md file is required")

    linked_documents = _llms_local_links(index_file, index_text)
    available = set(documents)
    missing = sorted(linked_documents - available)
    if missing:
        raise LlmsValidationError(
            index_file,
            f"local detail link does not exist: {missing[0]}",
        )
    orphaned = sorted(available - linked_documents)
    if orphaned:
        raise LlmsValidationError(
            docs_dir / orphaned[0],
            "detail is not reachable from the llms index",
        )
    return sorted(documents), index_bytes


def _validate_llms_index_format(index_file: Path, index_text: str) -> None:
    lines = index_text.splitlines()
    if lines:
        lines[0] = lines[0].removeprefix("\ufeff")
    if not lines or not re.match(r"^#[ \t]+\S", lines[0]) or lines[0].startswith("##"):
        raise LlmsValidationError(
            index_file,
            "first line must be a level-one Markdown H1",
        )
    blockquote_line = next(
        (line for line in lines[1:] if line.strip()),
        None,
    )
    if blockquote_line is None or not re.match(r"^>[ \t]?\S", blockquote_line):
        raise LlmsValidationError(
            index_file,
            "second line must be a Markdown blockquote",
        )
    if not any(
        re.match(r"^##[ \t]+\S", line) and not line.startswith("###")
        for line in lines[2:]
    ):
        raise LlmsValidationError(
            index_file,
            "index must contain at least one level-two Markdown H2",
        )


def _llms_local_links(index_file: Path, index_text: str) -> set[str]:
    links: set[str] = set()
    for match in _LLMS_LOCAL_LINK.finditer(index_text):
        target = match.group(1)
        if not _LLMS_DOCUMENT_NAME.fullmatch(target):
            raise LlmsValidationError(
                index_file,
                f"local link /llms/{target} must target a safe detail filename",
            )
        links.add(target)
    return links


def _validate_llms_directory(docs_dir: Path) -> None:
    try:
        directory_stat = docs_dir.lstat()
    except FileNotFoundError as error:
        raise LlmsValidationError(
            docs_dir, "detail directory does not exist"
        ) from error
    except OSError as error:
        raise LlmsValidationError(
            docs_dir,
            f"could not inspect detail directory ({_os_error_reason(error)})",
        ) from error
    if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
        raise LlmsValidationError(
            docs_dir,
            "detail directory must be a real, non-symlink directory",
        )


def _scan_llms_directory(docs_dir: Path) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(docs_dir) as entries:
            return list(entries)
    except FileNotFoundError as error:
        raise LlmsValidationError(
            docs_dir, "detail directory does not exist"
        ) from error
    except OSError as error:
        raise LlmsValidationError(
            docs_dir,
            f"could not read detail directory ({_os_error_reason(error)})",
        ) from error


def _read_llms_utf8(path: Path, *, source_label: str) -> bytes:
    """Read one bounded regular file without following a final symlink."""

    try:
        initial_stat = path.lstat()
    except FileNotFoundError as error:
        raise LlmsValidationError(path, f"{source_label} does not exist") from error
    except OSError as error:
        raise LlmsValidationError(
            path,
            f"could not inspect {source_label} ({_os_error_reason(error)})",
        ) from error
    if stat.S_ISLNK(initial_stat.st_mode):
        raise LlmsValidationError(path, f"{source_label} must not be a symlink")
    if not stat.S_ISREG(initial_stat.st_mode):
        raise LlmsValidationError(path, f"{source_label} must be a regular file")

    flags = os.O_RDONLY
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags |= nofollow
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise LlmsValidationError(
                    path, f"{source_label} must not be a symlink"
                ) from error
            raise LlmsValidationError(
                path,
                f"could not open {source_label} ({_os_error_reason(error)})",
            ) from error
        descriptor_stat = os.fstat(descriptor)
        if not stat.S_ISREG(descriptor_stat.st_mode):
            raise LlmsValidationError(path, f"{source_label} must be a regular file")
        if descriptor_stat.st_size > LLMS_MAX_BYTES:
            raise LlmsValidationError(
                path,
                f"{source_label} exceeds the {LLMS_MAX_BYTES // 1024} KiB limit",
            )

        chunks: list[bytes] = []
        total = 0
        while total <= LLMS_MAX_BYTES:
            chunk = os.read(descriptor, min(64 * 1024, LLMS_MAX_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > LLMS_MAX_BYTES:
                raise LlmsValidationError(
                    path,
                    f"{source_label} exceeds the {LLMS_MAX_BYTES // 1024} KiB limit",
                )
        final_stat = os.fstat(descriptor)
        if (
            final_stat.st_dev != descriptor_stat.st_dev
            or final_stat.st_ino != descriptor_stat.st_ino
            or not stat.S_ISREG(final_stat.st_mode)
        ):
            raise LlmsValidationError(path, f"{source_label} changed while being read")
        if final_stat.st_size > LLMS_MAX_BYTES:
            raise LlmsValidationError(
                path,
                f"{source_label} exceeds the {LLMS_MAX_BYTES // 1024} KiB limit",
            )
        data = b"".join(chunks)
    except LlmsValidationError:
        raise
    except OSError as error:
        raise LlmsValidationError(
            path,
            f"could not read {source_label} ({_os_error_reason(error)})",
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)

    try:
        data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise LlmsValidationError(
            path,
            f"{source_label} is not valid UTF-8",
        ) from error
    return data


def _os_error_reason(error: OSError) -> str:
    return error.strerror or type(error).__name__


class ToolframeError(Exception):
    """A structured, agent-visible tool failure.

    Tool authors may raise this exception to state whether a failure is safe to
    retry and to provide machine-readable recovery details.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}


class ToolLoadError(RuntimeError):
    """A fatal startup error associated with one tool and load phase."""

    def __init__(self, path: Path, phase: str, reason: str) -> None:
        self.path = path
        self.phase = phase
        self.reason = reason
        super().__init__(f"{path} [{phase}]: {reason}")


class RuntimeGuardMiddleware(Middleware):
    """Apply the global call limit, timeout, result cap, and error contract."""

    def __init__(
        self, concurrency: int, timeout_seconds: float, result_limit: int
    ) -> None:
        self._semaphore = anyio.Semaphore(concurrency) if concurrency > 0 else None
        self._timeout_seconds = timeout_seconds
        self._result_limit = result_limit

    async def on_call_tool(self, context: Any, call_next: Any) -> ToolResult:
        try:
            if self._semaphore is None:
                result = await self._invoke(call_next, context)
            else:
                async with self._semaphore:
                    result = await self._invoke(call_next, context)
            return self._enforce_result_limit(result)
        except TimeoutError:
            return error_result(
                "tool_timeout",
                f"The tool exceeded the best-effort {self._timeout_seconds:g}-second timeout.",
                retryable=True,
                details={
                    "recoveryHint": "Retry once or reduce the requested work. Blocking native code may continue in the background.",
                },
            )
        except (
            BaseException
        ) as error:  # Keep tool failures from terminating the server.
            if isinstance(error, anyio.get_cancelled_exc_class()):
                raise
            structured_error = _find_structured_tool_error(error)
            if structured_error is not None:
                LOGGER.warning(
                    "tool reported structured error code=%s retryable=%s",
                    structured_error.code,
                    structured_error.retryable,
                    exc_info=True,
                )
                details = dict(structured_error.details)
                recovery_hint = getattr(structured_error, "recovery_hint", None)
                if recovery_hint:
                    details.setdefault("recoveryHint", recovery_hint)
                return error_result(
                    structured_error.code,
                    structured_error.message,
                    retryable=structured_error.retryable,
                    details=details,
                    exception=structured_error,
                    trace_id=getattr(structured_error, "trace_id", None),
                )
            LOGGER.exception("unexpected tool invocation failure")
            exception_name = type(error).__name__
            validation = exception_name == "ValidationError"
            not_found = exception_name == "NotFoundError"
            if validation:
                code = "invalid_arguments"
                message = "The tool arguments did not satisfy its input schema."
                recovery_hint = (
                    "Correct the arguments using the published input schema."
                )
            elif not_found:
                code = "tool_not_found"
                message = "The requested tool is not present in the startup catalog."
                recovery_hint = (
                    "List tools again and use a tool loaded at process startup."
                )
            else:
                code = "internal_error"
                message = "The tool encountered an unexpected server error."
                recovery_hint = "Use the traceId to inspect the server log before deciding whether to retry."
            return error_result(
                code,
                message,
                retryable=False,
                details={
                    "exceptionType": exception_name,
                    "recoveryHint": recovery_hint,
                },
                exception=error,
            )

    async def _invoke(self, call_next: Any, context: Any) -> ToolResult:
        if self._timeout_seconds <= 0:
            return await call_next(context)
        with anyio.fail_after(self._timeout_seconds):
            return await call_next(context)

    def _enforce_result_limit(self, result: ToolResult) -> ToolResult:
        try:
            payload = result.to_mcp_result()
            size = len(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    default=_pydantic_json,
                ).encode("utf-8")
            )
        except BaseException as error:
            LOGGER.exception("could not serialize tool result")
            return error_result(
                "invalid_result",
                "The tool returned a result that cannot be encoded for MCP.",
                retryable=False,
                details={
                    "exceptionType": type(error).__name__,
                    "recoveryHint": "Return JSON-compatible content from the tool.",
                },
                exception=error,
            )

        if size > self._result_limit:
            return error_result(
                "result_too_large",
                f"The encoded tool result exceeded the {self._result_limit}-byte limit.",
                retryable=False,
                details={
                    "encodedBytes": size,
                    "limitBytes": self._result_limit,
                    "recoveryHint": "Return fewer rows or a smaller payload.",
                },
            )
        return result


class LlmsMethodGuardMiddleware:
    """Keep non-standard methods on discovery paths out of Streamlit."""

    def __init__(
        self,
        app: Any,
        audit_sink: StructuredLogs | None = None,
        fatal_reporter: FatalShutdownReporter | None = None,
    ) -> None:
        self.app = app
        self.audit_sink = audit_sink
        self.fatal_reporter = fatal_reporter

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("method") in _STANDARD_HTTP_METHODS:
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        index = path == "/llms.txt"
        discovery_path = (
            index or path == "/llms" or path.startswith(("/llms.txt/", "/llms/"))
        )
        if not discovery_path:
            await self.app(scope, receive, send)
            return

        status_code = 405 if index else 404
        headers = _llms_common_headers()
        if index:
            headers["Allow"] = "GET, HEAD"
        self._audit_rejection(scope, path, status_code)
        await PlainTextResponse(
            "Method Not Allowed" if index else "Not Found",
            status_code=status_code,
            headers=headers,
        )(scope, receive, send)

    def _audit_rejection(
        self, scope: dict[str, Any], path: str, status_code: int
    ) -> None:
        if self.audit_sink is None:
            return
        try:
            self.audit_sink.emit(
                "audit",
                "unsupported_llms_method",
                level="WARNING",
                outcome="rejected",
                statusCode=status_code,
                payload={"path": path, "method": scope.get("method")},
                **_transport_fields(scope, _scope_headers(scope)),
            )
        except StructuredLogError as error:
            if self.fatal_reporter is not None:
                self.fatal_reporter.logging_failure(error)
            raise


class HostOriginMiddleware:
    """Validate Host and browser Origin headers for HTTP and WebSocket routes."""

    def __init__(
        self,
        app: Any,
        allowed_hosts: list[str],
        allowed_origins: list[str],
        audit_sink: StructuredLogs | None = None,
        fatal_reporter: FatalShutdownReporter | None = None,
    ) -> None:
        self.app = app
        self.allowed_hosts = {host.casefold() for host in allowed_hosts}
        self.allowed_origins = {origin.casefold() for origin in allowed_origins}
        self.audit_sink = audit_sink
        self.fatal_reporter = fatal_reporter

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        scope_type = scope.get("type")
        if scope_type not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").casefold(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        host = _host_without_port(headers.get("host", "")).casefold()
        if not host or host not in self.allowed_hosts:
            self._audit_rejection(scope, headers, "host_rejected", host=host or None)
            if scope_type == "websocket":
                await send(
                    {
                        "type": "websocket.close",
                        "code": 1008,
                        "reason": "Unrecognized Host header",
                    }
                )
                return
            await PlainTextResponse("Unrecognized Host header", status_code=421)(
                scope, receive, send
            )
            return

        origin = headers.get("origin")
        if origin is not None and origin.casefold() not in self.allowed_origins:
            self._audit_rejection(scope, headers, "origin_rejected", origin=origin)
            if scope_type == "websocket":
                await send(
                    {
                        "type": "websocket.close",
                        "code": 1008,
                        "reason": "Origin is not allowed",
                    }
                )
                return
            await PlainTextResponse("Origin is not allowed", status_code=403)(
                scope, receive, send
            )
            return

        await self.app(scope, receive, send)

    def _audit_rejection(
        self,
        scope: dict[str, Any],
        headers: dict[str, str],
        event: str,
        **fields: Any,
    ) -> None:
        if self.audit_sink is None:
            return
        try:
            self.audit_sink.emit(
                "audit",
                event,
                level="WARNING",
                outcome="rejected",
                payload={"path": scope.get("path"), **fields},
                **_transport_fields(scope, headers),
            )
        except StructuredLogError as error:
            if self.fatal_reporter is not None:
                self.fatal_reporter.logging_failure(error)
            raise


class FatalShutdownReporter:
    """Emit the first fatal sentinel without depending on structured logging."""

    def __init__(self, *, killer: Any = os.kill) -> None:
        self._killer = killer
        self._lock = threading.Lock()
        self._reported = False

    @property
    def reported(self) -> bool:
        with self._lock:
            return self._reported

    def report(self, payload: dict[str, Any]) -> bool:
        with self._lock:
            if self._reported:
                return True
            self._reported = True
        print(
            f"{FATAL_SENTINEL} "
            + json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
            file=sys.stderr,
            flush=True,
        )
        sys.stderr.flush()
        self._killer(os.getpid(), signal.SIGTERM)
        return True

    def logging_failure(self, error: BaseException) -> bool:
        return self.report(
            {
                "traceId": uuid.uuid4().hex,
                "phase": "structured-logging",
                "exceptionType": type(error).__name__,
                "message": _safe_single_line(str(error), limit=1024),
                "retryable": False,
            }
        )


class McpTransportMiddleware:
    """Audit the exact streamable-HTTP endpoint at the ASGI boundary."""

    def __init__(
        self,
        app: Any,
        audit_sink: StructuredLogs,
        fatal_reporter: FatalShutdownReporter,
    ) -> None:
        self.app = app
        self.audit_sink = audit_sink
        self.fatal_reporter = fatal_reporter
        self._clients = _McpClientRegistry()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("path") != "/mcp":
            await self.app(scope, receive, send)
            return

        started = time.monotonic()
        operation_id = uuid.uuid4().hex
        headers = _scope_headers(scope)
        body = _BoundedBodyCapture()
        response_headers: dict[str, str] = {}
        status: int | None = None
        disconnected = False

        async def audited_receive() -> dict[str, Any]:
            nonlocal disconnected
            message = await receive()
            if message.get("type") == "http.request":
                body.add(message.get("body", b""))
            elif message.get("type") == "http.disconnect":
                disconnected = True
            return message

        async def audited_send(message: dict[str, Any]) -> None:
            nonlocal status
            if message.get("type") == "http.response.start":
                status = int(message["status"])
                response_headers.update(
                    {
                        key.decode("latin-1").casefold(): value.decode("latin-1")
                        for key, value in message.get("headers", [])
                    }
                )
            elif message.get("type") == "http.response.body":
                chunk = message.get("body", b"")
                if status is not None and status < 400 and chunk:
                    chunk_capture = _BoundedBodyCapture()
                    chunk_capture.add(chunk)
                    chunk_fields = _transport_fields(scope, headers)
                    request_session_id = chunk_fields["sessionId"]
                    response_session_id = response_headers.get("mcp-session-id")
                    chunk_fields["sessionId"] = (
                        request_session_id or response_session_id
                    )
                    chunk_fields["client"] = self._clients.resolve(
                        request_body=body.raw_if_complete(),
                        request_session_id=request_session_id,
                        response_session_id=response_session_id,
                    )
                    for wire_message in _mcp_wire_messages(
                        chunk_capture, response_headers.get("content-type")
                    ):
                        self.audit_sink.emit(
                            "mcp",
                            (
                                "mcp_wire_chunk"
                                if _is_bounded_wire_chunk(wire_message)
                                else "mcp_wire_message"
                            ),
                            operationId=operation_id,
                            protocolRequestId=_wire_request_id(wire_message),
                            direction="server_to_client",
                            statusCode=status,
                            outcome="success",
                            payload={
                                "message": wire_message,
                                "moreBody": bool(message.get("more_body", False)),
                            },
                            **chunk_fields,
                        )
            await send(message)

        caught: BaseException | None = None
        try:
            await self.app(scope, audited_receive, audited_send)
        except BaseException as error:  # noqa: BLE001 - audit cancellation/SystemExit too.
            caught = error
        if isinstance(caught, StructuredLogError):
            # The inner structured writer already invoked the fatal reporter.
            # Never attempt to describe a logging failure through that writer.
            self.fatal_reporter.logging_failure(caught)
            raise caught
        outcome = (
            "disconnected"
            if disconnected
            else "failed"
            if caught is not None or (status is not None and status >= 500)
            else "rejected"
            if status is not None and status >= 400
            else "success"
        )
        channel = "audit" if outcome in {"rejected", "failed"} else "mcp"
        event = (
            "session_close_requested"
            if scope.get("method") == "DELETE"
            else "mcp_transport_rejected"
            if outcome == "rejected"
            else "mcp_transport_completed"
        )
        caught_fields = _error_fields(caught) if caught is not None else None
        transport_fields = _transport_fields(scope, headers)
        response_session_id = response_headers.get("mcp-session-id")
        session_id = transport_fields["sessionId"] or response_session_id
        client = self._clients.resolve(
            request_body=body.raw_if_complete(),
            request_session_id=transport_fields["sessionId"],
            response_session_id=response_session_id,
        )
        transport_fields["sessionId"] = session_id
        transport_fields["client"] = client
        try:
            self.audit_sink.emit(
                channel,
                event,
                level="ERROR" if caught is not None else "INFO",
                operationId=operation_id,
                statusCode=status,
                outcome=outcome,
                durationMs=round((time.monotonic() - started) * 1000, 3),
                payload=(
                    {"requestBody": body.payload()} if outcome == "rejected" else None
                ),
                error=caught_fields,
                errorTraceId=(
                    caught_fields.get("errorTraceId")
                    if caught_fields is not None
                    else None
                ),
                **transport_fields,
            )
        except StructuredLogError as log_error:
            self.fatal_reporter.logging_failure(log_error)
            if caught is None:
                raise
        finally:
            if scope.get("method") == "DELETE" and session_id is not None:
                self._clients.remove(session_id)
        if caught is not None:
            raise caught


class McpAuditMiddleware(Middleware):
    """Record decoded FastMCP messages and final normalized outcomes."""

    def __init__(
        self, audit_sink: StructuredLogs, fatal_reporter: FatalShutdownReporter
    ) -> None:
        self.audit_sink = audit_sink
        self.fatal_reporter = fatal_reporter

    async def on_message(self, context: Any, call_next: Any) -> Any:
        operation_id = uuid.uuid4().hex
        method = context.method
        notification = context.type == "notification"
        paired = method in {"initialize", "tools/call"} and not notification
        common = _mcp_context_fields(context, operation_id)
        if not notification:
            self._emit(
                "mcp_message_started" if paired else "mcp_message_received",
                payload={"request": _jsonable(context.message)},
                outcome=None,
                **common,
            )
        started = time.monotonic()
        try:
            with idempotency._audit_context(
                operationId=common.get("operationId"),
                protocolRequestId=common.get("protocolRequestId"),
                tool=common.get("tool"),
                sessionId=common.get("sessionId"),
                client=common.get("client"),
            ):
                result = await call_next(context)
        except BaseException as error:
            cancelled = isinstance(error, anyio.get_cancelled_exc_class())
            timed_out = isinstance(error, TimeoutError)
            error_fields = _error_fields(error)
            self._emit(
                "mcp_message_completed",
                level="WARNING" if cancelled else "ERROR",
                outcome=(
                    "cancelled" if cancelled else "timeout" if timed_out else "failed"
                ),
                durationMs=round((time.monotonic() - started) * 1000, 3),
                error=error_fields,
                errorTraceId=error_fields["errorTraceId"],
                **_response_direction(common),
            )
            raise

        result_payload = _jsonable(result)
        outcome, retryable = _mcp_result_outcome(result_payload)
        result_trace_id = _mcp_result_trace_id(result_payload)
        self._emit(
            "mcp_notification" if notification else "mcp_message_completed",
            outcome=outcome,
            retryable=retryable,
            errorTraceId=result_trace_id,
            durationMs=round((time.monotonic() - started) * 1000, 3),
            payload={
                "message": _jsonable(context.message),
                "result": result_payload,
            },
            **(common if notification else _response_direction(common)),
        )
        return result

    def _emit(self, event: str, *, level: str = "INFO", **fields: Any) -> None:
        try:
            self.audit_sink.emit("mcp", event, level=level, **fields)
        except StructuredLogError as error:
            self.fatal_reporter.logging_failure(error)
            raise


class DashboardFatalReporter:
    """Report the first uncaught Streamlit script error and stop the process."""

    def __init__(
        self,
        dashboard_dir: Path,
        audit_sink: StructuredLogs | None = None,
        fatal_reporter: FatalShutdownReporter | None = None,
    ) -> None:
        self._dashboard_dir = dashboard_dir.resolve(strict=False)
        self._audit_sink = audit_sink
        self._fatal_reporter = fatal_reporter
        self._lock = threading.Lock()
        self._reported = False

    def __call__(self, error: BaseException) -> bool:
        # Streamlit normally keeps these exceptions away from the callback. The
        # guard is intentional so a direct invocation can never break rerun/stop.
        if _is_streamlit_control_exception(error):
            return False

        with self._lock:
            if self._reported:
                return True
            self._reported = True

        trace_id = uuid.uuid4().hex
        phase, path, widget = _dashboard_error_location(error, self._dashboard_dir)
        LOGGER.critical(
            "fatal dashboard error traceId=%s phase=%s path=%s widget=%s\n%s",
            trace_id,
            phase,
            path or "<unknown>",
            widget or "<none>",
            "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            ),
        )
        payload = {
            "traceId": trace_id,
            "phase": phase,
            "path": _safe_single_line(path) if path else None,
            "widget": _safe_single_line(widget) if widget else None,
            "exceptionType": type(error).__name__,
            "message": _safe_single_line(str(error), limit=512) or type(error).__name__,
            "retryable": False,
        }
        if self._audit_sink is not None:
            try:
                self._audit_sink.emit(
                    "audit",
                    "python_runtime_fatal",
                    level="ERROR",
                    outcome="failed",
                    error={
                        "type": type(error).__name__,
                        "message": str(error),
                        "traceback": "".join(
                            traceback.format_exception(
                                type(error), error, error.__traceback__
                            )
                        ),
                        "errorTraceId": trace_id,
                        "retryable": False,
                    },
                    errorTraceId=trace_id,
                    widget=widget,
                    payload={"phase": phase, "path": path},
                )
            except StructuredLogError as log_error:
                if self._fatal_reporter is not None:
                    self._fatal_reporter.logging_failure(log_error)
                    return True
                raise
        if self._fatal_reporter is not None:
            self._fatal_reporter.report(payload)
        else:
            print(
                f"{FATAL_SENTINEL} "
                + json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
                file=sys.stderr,
                flush=True,
            )
            sys.stderr.flush()
            os.kill(os.getpid(), signal.SIGTERM)
        return True


_DASHBOARD_FATAL_REPORTER: DashboardFatalReporter | None = None


def configure_dashboard_fatal_reporter(
    dashboard_dir: Path,
    audit_sink: StructuredLogs | None = None,
    fatal_reporter: FatalShutdownReporter | None = None,
) -> DashboardFatalReporter:
    global _DASHBOARD_FATAL_REPORTER
    reporter = DashboardFatalReporter(dashboard_dir, audit_sink, fatal_reporter)
    _DASHBOARD_FATAL_REPORTER = reporter
    return reporter


def report_dashboard_fatal(error: BaseException) -> bool:
    reporter = _DASHBOARD_FATAL_REPORTER
    if reporter is None:
        raise RuntimeError("dashboard fatal reporter is not configured") from error
    return reporter(error)


def error_result(
    code: str,
    message: str,
    *,
    retryable: bool,
    details: dict[str, Any] | None = None,
    exception: BaseException | None = None,
    trace_id: str | None = None,
) -> ToolResult:
    trace_id = trace_id or uuid.uuid4().hex
    error_details = dict(details or {})
    if exception is not None:
        error_details.setdefault("stack", _sanitized_stack(exception))
    payload = {
        "code": code,
        "message": message,
        "retryable": retryable,
        "details": error_details,
        "traceId": trace_id,
    }
    return ToolResult(
        content=f"{message} Recovery: {error_details.get('recoveryHint', 'Inspect the error details.')} [traceId={trace_id}]",
        structured_content={"error": payload},
        meta={"retryable": retryable, "traceId": trace_id},
        is_error=True,
    )


def load_tools(
    manifest_path: Path,
) -> tuple[list[Tool], list[dict[str, Any]], list[str]]:
    manifest = _read_manifest(manifest_path)
    tools_dir = Path(manifest["toolsDir"]).resolve(strict=True)
    if not tools_dir.is_dir():
        raise ToolLoadError(tools_dir, "manifest", "toolsDir is not a directory")

    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))

    loaded: list[Tool] = []
    runtime_details: list[dict[str, Any]] = []
    names: set[str] = set()
    dependencies: set[str] = set()
    for entry in manifest["tools"]:
        path = Path(entry.get("path", ""))
        try:
            if path.is_symlink():
                raise ToolLoadError(
                    path, "path", "tool path became a symlink after discovery"
                )
            path = path.resolve(strict=True)
            if path.parent != tools_dir or not path.is_file():
                raise ToolLoadError(
                    path,
                    "path",
                    "tool must be a direct, regular, non-symlink child of toolsDir",
                )
            if entry.get("sourceFilename") != path.name:
                raise ToolLoadError(
                    path, "manifest", "source filename does not match path"
                )

            source = path.read_bytes()
            actual_hash = hashlib.sha256(source).hexdigest()
            if actual_hash != entry.get("sourceSha256"):
                raise ToolLoadError(
                    path,
                    "integrity",
                    "source changed after discovery; restart after the file is stable",
                )

            expected_name = entry.get("name")
            if expected_name in names:
                raise ToolLoadError(
                    path, "identity", f"duplicate tool name {expected_name!r}"
                )
            module = _import_tool(path, expected_name, actual_hash)
            function = _one_decorated_function(path, module)
            if function.__name__ != expected_name:
                raise ToolLoadError(
                    path,
                    "identity",
                    f"decorated function {function.__name__!r} must match filename stem {expected_name!r}",
                )
            tool_object = Tool.from_function(function)
            if tool_object.name != expected_name:
                raise ToolLoadError(
                    path,
                    "identity",
                    f"published tool name {tool_object.name!r} must be {expected_name!r}",
                )
            if not (tool_object.description or "").strip():
                raise ToolLoadError(
                    path,
                    "schema",
                    "tool must have a non-empty docstring or description",
                )

            # Materialize schemas now so a malformed tool aborts before HTTP startup.
            tool_object.to_mcp_tool()
            loaded.append(tool_object)
            names.add(expected_name)
            entry_dependencies = list(entry.get("dependencies", []))
            dependencies.update(entry_dependencies)
            runtime_details.append(
                {
                    "name": expected_name,
                    "description": tool_object.description,
                    "sourceFile": path.name,
                    "dependencies": entry_dependencies,
                }
            )
        except ToolLoadError:
            raise
        except Exception as error:
            LOGGER.error(
                "tool load failed path=%s phase=import\n%s",
                path,
                traceback.format_exc(),
            )
            raise ToolLoadError(
                path, "import", f"{type(error).__name__}: {error}"
            ) from error

    return loaded, runtime_details, sorted(dependencies)


def build_application() -> FastAPI:
    logs_dir = _required_path("TOOLFRAME_LOGS_DIR")
    retention_days = _integer_setting("TOOLFRAME_LOG_RETENTION_DAYS", minimum=1)
    max_total_mib = _integer_setting("TOOLFRAME_LOG_MAX_TOTAL_MIB", minimum=1)
    audit_sink = StructuredLogs(
        StructuredLogSettings(
            logs_dir=logs_dir,
            retention_days=retention_days,
            max_total_mib=max_total_mib,
        )
    )
    fatal_reporter = FatalShutdownReporter()
    try:
        return _build_application(audit_sink, fatal_reporter)
    except BaseException as error:
        error_fields = _error_fields(error)
        try:
            audit_sink.emit(
                "audit",
                "python_runtime_fatal",
                level="ERROR",
                outcome="failed",
                error=error_fields,
                errorTraceId=error_fields["errorTraceId"],
                payload={"phase": "startup"},
            )
        except StructuredLogError as log_error:
            fatal_reporter.logging_failure(log_error)
        finally:
            try:
                audit_sink.close()
            except StructuredLogError as log_error:
                fatal_reporter.logging_failure(log_error)
        raise


def _build_application(
    audit_sink: StructuredLogs, fatal_shutdown: FatalShutdownReporter
) -> FastAPI:
    if sys.version_info[:2] != (3, 14) or sys.version_info.releaselevel != "final":
        raise RuntimeError(
            "toolframe requires a final Python 3.14 release; "
            f"found {sys.version.split()[0]} ({sys.version_info.releaselevel})"
        )
    manifest_path = _required_path("TOOLFRAME_MANIFEST")
    database_path = _required_path("TOOLFRAME_DB")
    llms_file = _required_path("TOOLFRAME_LLMS_FILE")
    llms_dir = _required_path("TOOLFRAME_LLMS_DIR")
    llms_documents = tuple(validate_llms_source(llms_file, llms_dir))
    dashboard_dir = _required_directory("TOOLFRAME_DASHBOARD_DIR")
    dashboard_app_path = dashboard_dir / "app.py"
    if not dashboard_app_path.is_file() or dashboard_app_path.is_symlink():
        raise RuntimeError(
            f"dashboard app must be a regular, non-symlink file: {dashboard_app_path}"
        )
    dashboard_runner_path = _required_file("TOOLFRAME_DASHBOARD_RUNNER")
    info_page_path = _required_file("TOOLFRAME_INFO_PAGE")
    workspace_path = _required_directory("TOOLFRAME_WORKSPACE")
    allowed_hosts = _json_string_list("TOOLFRAME_ALLOWED_HOSTS")
    allowed_origins = _json_string_list("TOOLFRAME_ALLOWED_ORIGINS")
    concurrency = _integer_setting("TOOLFRAME_MAX_CONCURRENCY", minimum=0)
    timeout_seconds = _float_setting("TOOLFRAME_TOOL_TIMEOUT_SECONDS", minimum=0)
    result_limit = _integer_setting("TOOLFRAME_RESULT_LIMIT_BYTES", minimum=1)
    port = _integer_setting("TOOLFRAME_PORT", minimum=1)
    if port > 65535:
        raise RuntimeError("TOOLFRAME_PORT must be at most 65535")
    ready_token = os.environ.get("TOOLFRAME_READY_TOKEN", "").strip()
    if not ready_token:
        raise RuntimeError("TOOLFRAME_READY_TOKEN is required")

    database_sdk._configure(
        database_path,
        logger=logging.getLogger("toolframe.database"),
    )
    idempotency._configure(database_path, audit_sink=audit_sink)
    artifact_store = configure_artifact_store(
        workspace_path,
        max_artifact_bytes=DEFAULT_MAX_ARTIFACT_BYTES,
        audit_sink=audit_sink,
        logger=logging.getLogger("toolframe.artifacts"),
    )
    tools, tool_details, tool_dependencies = load_tools(manifest_path)
    dependency_summary = _json_string_list("TOOLFRAME_DEPENDENCIES_JSON", required=False)
    if not dependency_summary:
        dependency_summary = tool_dependencies

    streamlit = _load_and_configure_streamlit(
        dashboard_dir=dashboard_dir,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        port=port,
    )
    runtime_summary = _runtime_summary(
        dashboard_dir=dashboard_dir,
        tool_details=tool_details,
        dependency_summary=dependency_summary,
        concurrency=concurrency,
        timeout_seconds=timeout_seconds,
        result_limit=result_limit,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        python_version=sys.version.split()[0],
        agent_documents=list(llms_documents),
    )

    import toolframe_dashboard as dashboard_sdk

    dashboard_sdk._configure(
        database_path=database_path,
        dashboard_dir=dashboard_dir,
        workspace=workspace_path,
        logger=logging.getLogger("toolframe.dashboard"),
        runtime=runtime_summary,
        audit_sink=audit_sink,
        artifacts=artifact_store,
    )
    # Import-safe preflight intentionally does not render the app. Rendering is
    # session-scoped and may have database side effects.
    dashboard_sdk.preflight_dashboard(dashboard_dir)

    mcp = FastMCP(
        "toolframe",
        version=os.environ.get("TOOLFRAME_VERSION", "0.0.0"),
        strict_input_validation=True,
    )
    for tool_object in tools:
        mcp.add_tool(tool_object)
    mcp.add_middleware(McpAuditMiddleware(audit_sink, fatal_shutdown))
    mcp.add_middleware(
        RuntimeGuardMiddleware(concurrency, timeout_seconds, result_limit)
    )

    mcp_app = mcp.http_app(
        path="/mcp",
        transport="streamable-http",
        stateless_http=False,
        host_origin_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )
    fatal_reporter = configure_dashboard_fatal_reporter(
        dashboard_dir, audit_sink, fatal_shutdown
    )
    streamlit_app = streamlit.App(
        str(dashboard_runner_path),
        on_script_error=fatal_reporter,
        debug=False,
    )

    @asynccontextmanager
    async def readiness_lifespan(_: FastAPI):
        thread_limiter = anyio.to_thread.current_default_thread_limiter()
        previous_thread_tokens = thread_limiter.total_tokens
        thread_limiter.total_tokens = math.inf if concurrency == 0 else concurrency
        try:
            try:
                audit_sink.emit(
                    "audit",
                    "python_runtime_started",
                    outcome="success",
                    payload={"pythonVersion": sys.version.split()[0]},
                )
            except StructuredLogError as error:
                fatal_shutdown.logging_failure(error)
                raise
            print(f"{READY_SENTINEL} {ready_token}", flush=True)
            yield
        finally:
            thread_limiter.total_tokens = previous_thread_tokens

    combined_lifespan = combine_lifespans(
        mcp_app.lifespan,
        streamlit_app.lifespan(),
        readiness_lifespan,
    )

    @asynccontextmanager
    async def application_lifespan(application: FastAPI):
        failure: BaseException | None = None
        try:
            async with combined_lifespan(application):
                yield
        except BaseException as error:
            failure = error
            if not isinstance(error, StructuredLogError):
                error_fields = _error_fields(error)
                try:
                    audit_sink.emit(
                        "audit",
                        "python_runtime_fatal",
                        level="ERROR",
                        outcome="failed",
                        error=error_fields,
                        errorTraceId=error_fields["errorTraceId"],
                        payload={"phase": "lifespan"},
                    )
                except StructuredLogError as log_error:
                    fatal_shutdown.logging_failure(log_error)
            raise
        finally:
            if not isinstance(failure, StructuredLogError):
                try:
                    audit_sink.emit(
                        "audit",
                        "python_runtime_stopped",
                        outcome=(
                            "failed"
                            if failure is not None or fatal_shutdown.reported
                            else "success"
                        ),
                    )
                except StructuredLogError as error:
                    fatal_shutdown.logging_failure(error)
            try:
                audit_sink.close()
            except StructuredLogError as error:
                fatal_shutdown.logging_failure(error)

    app = FastAPI(
        title="toolframe",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=application_lifespan,
    )
    app.add_middleware(
        LlmsMethodGuardMiddleware,
        audit_sink=audit_sink,
        fatal_reporter=fatal_shutdown,
    )
    app.add_middleware(
        McpTransportMiddleware,
        audit_sink=audit_sink,
        fatal_reporter=fatal_shutdown,
    )
    app.add_middleware(
        HostOriginMiddleware,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        audit_sink=audit_sink,
        fatal_reporter=fatal_shutdown,
    )

    info_page = info_page_path.read_text(encoding="utf-8")

    @app.get("/info", response_class=HTMLResponse)
    async def info() -> HTMLResponse:
        return HTMLResponse(info_page)

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/database")
    async def database_snapshot() -> JSONResponse:
        try:
            snapshot = await anyio.to_thread.run_sync(_database_snapshot, database_path)
            return JSONResponse(snapshot)
        except Exception:
            trace_id = uuid.uuid4().hex
            LOGGER.exception("database preview failed traceId=%s", trace_id)
            return JSONResponse(
                {"error": "Database preview failed.", "traceId": trace_id},
                status_code=500,
            )

    @app.get("/api/runtime")
    async def runtime_snapshot() -> dict[str, Any]:
        return runtime_summary

    def _current_llms_source() -> tuple[list[str], bytes]:
        current_documents, index_bytes = _validated_llms_source(llms_file, llms_dir)
        if tuple(current_documents) != llms_documents:
            raise LlmsValidationError(
                llms_file,
                "validated detail allowlist changed since startup",
            )
        return current_documents, index_bytes

    @app.api_route("/llms.txt", methods=["GET", "HEAD"])
    async def llms_index() -> Response:
        try:
            _, index_bytes = _current_llms_source()
            return _llms_response(index_bytes)
        except LlmsValidationError as error:
            return _llms_unavailable(error)

    @app.api_route(
        "/llms.txt",
        methods=["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"],
    )
    async def reject_llms_index_method(request: Request) -> PlainTextResponse:
        _emit_route_rejection(
            audit_sink,
            fatal_shutdown,
            "unsupported_llms_method",
            request=request,
            payload={"unsupportedPath": "llms.txt"},
        )
        return PlainTextResponse(
            "Method Not Allowed",
            status_code=405,
            headers={**_llms_common_headers(), "Allow": "GET, HEAD"},
        )

    @app.api_route(
        "/llms.txt/{unsupported_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "TRACE"],
    )
    async def reject_unsupported_llms_index_path(
        unsupported_path: str, request: Request
    ) -> PlainTextResponse:
        _emit_route_rejection(
            audit_sink,
            fatal_shutdown,
            "unsupported_llms_path",
            request=request,
            payload={"unsupportedPath": f"llms.txt/{unsupported_path}"},
        )
        return PlainTextResponse(
            "Not Found", status_code=404, headers=_llms_common_headers()
        )

    @app.api_route("/llms/{document_name}.md", methods=["GET", "HEAD"])
    async def llms_detail(document_name: str) -> Response:
        filename = f"{document_name}.md"
        if filename not in llms_documents or not _LLMS_DOCUMENT_NAME.fullmatch(
            filename
        ):
            return PlainTextResponse(
                "Not Found",
                status_code=404,
                headers=_llms_common_headers(),
            )
        try:
            _current_llms_source()
            detail_bytes = _read_llms_utf8(
                llms_dir / filename,
                source_label="llms detail",
            )
            return _llms_response(detail_bytes, detail=True)
        except LlmsValidationError as error:
            return _llms_unavailable(error)

    @app.api_route(
        "/llms",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "TRACE"],
    )
    async def reject_llms_root(request: Request) -> PlainTextResponse:
        _emit_route_rejection(
            audit_sink,
            fatal_shutdown,
            "unsupported_llms_path",
            request=request,
            payload={"unsupportedPath": ""},
        )
        return PlainTextResponse(
            "Not Found", status_code=404, headers=_llms_common_headers()
        )

    @app.api_route(
        "/llms/{unsupported_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "TRACE"],
    )
    async def reject_unsupported_llms_path(
        unsupported_path: str, request: Request
    ) -> PlainTextResponse:
        _emit_route_rejection(
            audit_sink,
            fatal_shutdown,
            "unsupported_llms_path",
            request=request,
            payload={"unsupportedPath": unsupported_path},
        )
        return PlainTextResponse(
            "Not Found", status_code=404, headers=_llms_common_headers()
        )

    # Route the full FastMCP ASGI application at the exact endpoint instead of
    # using Mount("/mcp"), which redirects POST /mcp to /mcp/. This retains the
    # FastMCP request-context and Host/Origin middleware while allowing the root
    # mount to belong entirely to Streamlit.
    app.router.routes.append(Route("/mcp", mcp_app))

    @app.api_route(
        "/mcp/{unsupported_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def reject_unsupported_mcp_path(
        unsupported_path: str, request: Request
    ) -> PlainTextResponse:
        _emit_route_rejection(
            audit_sink,
            fatal_shutdown,
            "unsupported_mcp_path",
            request=request,
            payload={"unsupportedPath": unsupported_path},
        )
        return PlainTextResponse("Not Found", status_code=404)

    @app.api_route(
        "/sse",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def reject_legacy_sse(request: Request) -> PlainTextResponse:
        _emit_route_rejection(
            audit_sink,
            fatal_shutdown,
            "legacy_sse_rejected",
            request=request,
        )
        return PlainTextResponse("Not Found", status_code=404)

    app.mount("/", streamlit_app)
    return app


def _load_and_configure_streamlit(
    *,
    dashboard_dir: Path,
    allowed_hosts: list[str],
    allowed_origins: list[str],
    port: int,
) -> Any:
    import streamlit
    from streamlit import config as streamlit_config

    if streamlit.__version__ != STREAMLIT_VERSION or not hasattr(streamlit, "App"):
        raise RuntimeError(
            f"toolframe requires streamlit=={STREAMLIT_VERSION} with st.App; "
            f"found {getattr(streamlit, '__version__', '<unknown>')}"
        )
    for key, value in _streamlit_config_values(
        dashboard_dir=dashboard_dir,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        port=port,
    ).items():
        streamlit_config.set_option(key, value, where_defined="toolframe runtime")
    return streamlit


def _streamlit_config_values(
    *,
    dashboard_dir: Path,
    allowed_hosts: list[str],
    allowed_origins: list[str],
    port: int,
) -> dict[str, Any]:
    if not dashboard_dir.is_absolute():
        raise RuntimeError("the Streamlit dashboard watch directory must be absolute")
    return {
        "server.folderWatchList": [str(dashboard_dir)],
        "server.runOnSave": False,
        "server.headless": True,
        "browser.gatherUsageStats": False,
        "client.toolbarMode": "minimal",
        "client.showErrorDetails": "none",
        "server.allowedHosts": list(allowed_hosts),
        "server.corsAllowedOrigins": list(allowed_origins),
        "server.enableCORS": True,
        "server.enableXsrfProtection": True,
        "server.maxUploadSize": STREAMLIT_RESOURCE_LIMIT_MIB,
        "server.maxMessageSize": STREAMLIT_RESOURCE_LIMIT_MIB,
        "server.maxWidgetStateSize": STREAMLIT_RESOURCE_LIMIT_MIB,
        "server.port": port,
        "browser.serverPort": port,
        "server.baseUrlPath": "",
    }


def _runtime_summary(
    *,
    dashboard_dir: Path,
    tool_details: list[dict[str, Any]],
    dependency_summary: list[str],
    concurrency: int,
    timeout_seconds: float,
    result_limit: int,
    allowed_hosts: list[str],
    allowed_origins: list[str],
    python_version: str,
    agent_documents: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "pythonVersion": python_version,
        "tools": tool_details,
        "dependencies": dependency_summary,
        "agentDocuments": list(agent_documents or []),
        "startupOnly": True,
        "maxConcurrency": concurrency,
        "toolTimeoutSeconds": timeout_seconds,
        "resultLimitBytes": result_limit,
        "dashboard": {
            "framework": f"Streamlit {STREAMLIT_VERSION}",
            "directory": str(dashboard_dir),
            "entrypoint": "app.py",
            "sourceLifecycle": "browser refresh or Streamlit rerun",
            "dependencyLifecycle": "process restart",
            "failurePolicy": "uncaught errors stop toolframe",
        },
        "security": {
            "authentication": "none",
            "networkScope": "LAN",
            "trustedDashboardCode": True,
            "allowedHosts": list(allowed_hosts),
            "allowedOrigins": list(allowed_origins),
            "maxUploadMiB": STREAMLIT_RESOURCE_LIMIT_MIB,
            "maxWebSocketMessageMiB": STREAMLIT_RESOURCE_LIMIT_MIB,
            "warning": "Dashboard data is visible to every client allowed on this LAN service.",
        },
        "lifecycle": {
            "tools": "loaded once at process startup",
            "dashboardSource": "loaded on each Streamlit run",
            "reloadApi": False,
            "automaticProcessRestart": False,
        },
    }


def _llms_common_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }


def _llms_response(content: bytes, *, detail: bool = False) -> Response:
    headers = _llms_common_headers()
    if detail:
        headers["Link"] = '</llms.txt>; rel="describedby"'
    return Response(content=content, media_type="text/markdown", headers=headers)


def _llms_unavailable(error: LlmsValidationError) -> PlainTextResponse:
    LOGGER.error("llms source error source=%s reason=%s", error.source, error.reason)
    return PlainTextResponse(
        f"llms source unavailable: {error.reason}",
        status_code=503,
        headers=_llms_common_headers(),
    )


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:
        raise ToolLoadError(
            path, "manifest", f"{type(error).__name__}: {error}"
        ) from error
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ToolLoadError(path, "manifest", "unsupported or missing manifest version")
    if manifest.get("pythonVersion") != "3.14.0":
        raise ToolLoadError(path, "manifest", "runtime requires Python 3.14.0")
    if not isinstance(manifest.get("tools"), list):
        raise ToolLoadError(path, "manifest", "tools must be an array")
    return manifest


def _import_tool(path: Path, name: str, source_hash: str) -> types.ModuleType:
    module_name = f"toolframe_user_{name}_{source_hash[:16]}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ToolLoadError(
            path, "import", "could not create a Python module specification"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def _one_decorated_function(path: Path, module: types.ModuleType) -> Any:
    decorated = []
    for value in vars(module).values():
        if not inspect.isfunction(value) or value.__module__ != module.__name__:
            continue
        if isinstance(get_fastmcp_meta(value), ToolMeta):
            decorated.append(value)
    unique = list({id(function): function for function in decorated}.values())
    if len(unique) != 1:
        raise ToolLoadError(
            path,
            "registration",
            f"expected exactly one locally defined @tool function, found {len(unique)}",
        )
    return unique[0]


def _database_snapshot(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(path)
    try:
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        tables = []
        for name in names:
            identifier = '"' + name.replace('"', '""') + '"'
            cursor = connection.execute(
                f"SELECT * FROM {identifier} LIMIT ?", (DATABASE_PREVIEW_ROWS + 1,)
            )
            rows = cursor.fetchall()
            tables.append(
                {
                    "name": name,
                    "columns": [
                        description[0] for description in cursor.description or []
                    ],
                    "rows": [
                        [_json_value(value) for value in row]
                        for row in rows[:DATABASE_PREVIEW_ROWS]
                    ],
                    "truncated": len(rows) > DATABASE_PREVIEW_ROWS,
                }
            )
        return {
            "databasePath": str(path),
            "generatedAt": datetime.now(UTC).isoformat(),
            "tables": tables,
        }
    finally:
        connection.close()


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<blob: {len(value)} bytes>"
    return str(value)


def _pydantic_json(value: Any) -> Any:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _scope_headers(scope: dict[str, Any]) -> dict[str, str]:
    return {
        key.decode("latin-1").casefold(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }


class _BoundedBodyCapture:
    """Hash an ASGI body while retaining only bounded head/tail previews."""

    # Leave room for base64 expansion and metadata so the complete preview can
    # pass through the structured writer's 16 KiB payload bound unchanged.
    _PART_LIMIT = 5 * 1024

    def __init__(self) -> None:
        self._head = bytearray()
        self._tail = bytearray()
        self._hash = hashlib.sha256()
        self._size = 0

    def add(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._hash.update(chunk)
        self._size += len(chunk)
        head_room = self._PART_LIMIT - len(self._head)
        if head_room > 0:
            self._head.extend(chunk[:head_room])
        self._tail.extend(chunk)
        if len(self._tail) > self._PART_LIMIT:
            del self._tail[: len(self._tail) - self._PART_LIMIT]

    def raw_if_complete(self) -> bytes | None:
        """Return the exact body only while it fits the retained head/tail."""

        if self._size <= self._PART_LIMIT:
            return bytes(self._head)
        if self._size <= self._PART_LIMIT * 2:
            overlap = self._PART_LIMIT * 2 - self._size
            return bytes(self._head) + bytes(self._tail[overlap:])
        return None

    def payload(self) -> dict[str, Any]:
        complete = self.raw_if_complete()
        if complete is not None:
            preview = complete
            truncated = False
        else:
            preview = (
                bytes(self._head)
                + b"\n... <middle truncated> ...\n"
                + bytes(self._tail)
            )
            truncated = True
        try:
            display: Any = preview.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            display = base64.b64encode(preview).decode("ascii")
            encoding = "base64"
        return {
            "preview": display,
            "encoding": encoding,
            "truncated": truncated,
            "originalBytes": self._size,
            "sha256": self._hash.hexdigest(),
        }


class _McpClientRegistry:
    """Correlate self-declared client identity with wire session IDs."""

    _MAX_SESSIONS = 4_096

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, dict[str, Any]] = {}

    def resolve(
        self,
        *,
        request_body: bytes | None,
        request_session_id: str | None,
        response_session_id: str | None,
    ) -> dict[str, Any]:
        declared = _declared_mcp_client(request_body)
        with self._lock:
            known = self._sessions.get(request_session_id or "")
            client = declared or known or _unknown_mcp_client()
            if response_session_id and declared is not None:
                if len(self._sessions) >= self._MAX_SESSIONS:
                    self._sessions.pop(next(iter(self._sessions)))
                self._sessions[response_session_id] = dict(declared)
            return dict(client)

    def remove(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)


def _unknown_mcp_client() -> dict[str, Any]:
    return {"name": None, "version": None, "verified": False}


def _declared_mcp_client(body: bytes | None) -> dict[str, Any] | None:
    if not body:
        return None
    try:
        decoded = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    messages = decoded if isinstance(decoded, list) else [decoded]
    for message in messages:
        if not isinstance(message, dict) or message.get("method") != "initialize":
            continue
        params = message.get("params")
        if not isinstance(params, dict):
            continue
        client_info = params.get("clientInfo", params.get("client_info"))
        if not isinstance(client_info, dict):
            continue
        name = client_info.get("name")
        version = client_info.get("version")
        return {
            "name": name if isinstance(name, str) else None,
            "version": version if isinstance(version, str) else None,
            "verified": False,
        }
    return None


def _mcp_wire_messages(
    capture: _BoundedBodyCapture, content_type: str | None
) -> list[Any]:
    """Decode complete JSON/SSE responses or retain one bounded wire record."""

    raw = capture.raw_if_complete()
    if raw is None:
        return [{"wire": capture.payload()}]
    media_type = (content_type or "").partition(";")[0].strip().casefold()
    try:
        if media_type == "text/event-stream":
            messages: list[Any] = []
            normalized = raw.replace(b"\r\n", b"\n")
            for block in normalized.split(b"\n\n"):
                data_lines = [
                    line[5:].lstrip()
                    for line in block.splitlines()
                    if line.startswith(b"data:")
                ]
                if data_lines:
                    messages.append(json.loads(b"\n".join(data_lines)))
            if messages:
                return messages
        elif media_type == "application/json":
            decoded = json.loads(raw)
            return decoded if isinstance(decoded, list) else [decoded]
        else:
            return []
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    return [{"wire": capture.payload()}]


def _wire_request_id(message: Any) -> Any:
    if isinstance(message, dict):
        return message.get("id")
    return None


def _is_bounded_wire_chunk(message: Any) -> bool:
    return isinstance(message, dict) and set(message) == {"wire"}


def _transport_fields(scope: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    client = scope.get("client")
    source_ip = client[0] if isinstance(client, (tuple, list)) and client else None
    source_port = (
        client[1] if isinstance(client, (tuple, list)) and len(client) > 1 else None
    )
    return {
        "sourceIp": source_ip,
        "sourcePort": source_port,
        "sessionId": headers.get("mcp-session-id"),
        "client": _unknown_mcp_client(),
        "httpMethod": scope.get("method"),
        "path": scope.get("path"),
        "contentType": headers.get("content-type"),
        "contentLength": headers.get("content-length"),
        "userAgent": headers.get("user-agent"),
        "protocolVersion": headers.get("mcp-protocol-version"),
    }


def _mcp_context_fields(context: Any, operation_id: str) -> dict[str, Any]:
    fastmcp_context = context.fastmcp_context
    request_context = (
        fastmcp_context.request_context if fastmcp_context is not None else None
    )
    protocol_request_id = (
        request_context.request_id if request_context is not None else None
    )
    meta = request_context.meta if request_context is not None else None
    otel_trace_id = _current_otel_trace_id()

    source_ip = source_port = session_id = user_agent = protocol_version = None
    try:
        from fastmcp.server.dependencies import get_http_request

        request = get_http_request()
        if request.client is not None:
            source_ip = request.client.host
            source_port = request.client.port
        session_id = request.headers.get("mcp-session-id")
        user_agent = request.headers.get("user-agent")
        protocol_version = request.headers.get("mcp-protocol-version")
    except RuntimeError:
        pass

    client_name = client_version = None
    if fastmcp_context is not None:
        try:
            client_params = fastmcp_context.session.client_params
        except RuntimeError:
            client_params = None
        client_info = getattr(client_params, "client_info", None)
        client_name = getattr(client_info, "name", None)
        client_version = getattr(client_info, "version", None)
    message = context.message
    if context.method == "initialize" and client_name is None:
        initialize_params = getattr(message, "params", message)
        client_info = getattr(initialize_params, "client_info", None)
        client_name = getattr(client_info, "name", None)
        client_version = getattr(client_info, "version", None)
    if client_name is None and isinstance(meta, dict):
        modern_info = meta.get("io.modelcontextprotocol/clientInfo")
        if isinstance(modern_info, dict):
            client_name = modern_info.get("name")
            client_version = modern_info.get("version")
    tool = getattr(message, "name", None)
    if tool is None:
        params = getattr(message, "params", None)
        tool = getattr(params, "name", None)
    return {
        "operationId": operation_id,
        "protocolRequestId": protocol_request_id,
        "sessionId": session_id,
        "sourceIp": source_ip,
        "sourcePort": source_port,
        "client": {
            "name": client_name,
            "version": client_version,
            "verified": False,
        },
        "userAgent": user_agent,
        "method": context.method,
        "tool": tool,
        "direction": (
            "client_to_server" if context.source == "client" else "server_to_client"
        ),
        "otelTraceId": otel_trace_id,
        "protocolVersion": protocol_version,
    }


def _response_direction(fields: dict[str, Any]) -> dict[str, Any]:
    response = dict(fields)
    response["direction"] = (
        "client_to_server"
        if fields.get("direction") == "server_to_client"
        else "server_to_client"
    )
    return response


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool, bytes)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    # Component-list middleware receives FastMCP's internal component models,
    # which deliberately retain implementation callables that are not part of
    # the wire response. Convert those models through their public MCP
    # representation before asking Pydantic to serialize them.
    for converter_name in (
        "to_mcp_tool",
        "to_mcp_prompt",
        "to_mcp_resource",
        "to_mcp_template",
    ):
        converter = getattr(value, converter_name, None)
        if callable(converter):
            return _jsonable(converter())
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(mode="json")
        except (TypeError, ValueError):
            # Some otherwise valid Pydantic models contain opaque runtime
            # values. Python mode lets the recursive fallback below describe
            # them without breaking the MCP operation being audited.
            dumped = model_dump(mode="python")
        return _jsonable(dumped)
    to_mcp_result = getattr(value, "to_mcp_result", None)
    if callable(to_mcp_result):
        return _jsonable(to_mcp_result())
    if hasattr(value, "__dict__"):
        return {
            key: _jsonable(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return str(value)


def _mcp_result_outcome(payload: Any) -> tuple[str, bool | None]:
    if not isinstance(payload, dict):
        return "success", None
    error = payload.get("structuredContent", payload.get("structured_content"))
    if isinstance(error, dict):
        error = error.get("error")
    is_error = payload.get("isError", payload.get("is_error", False)) is True
    if not isinstance(error, dict):
        return ("failed", None) if is_error else ("success", None)
    retryable = error.get("retryable")
    code = error.get("code")
    return ("timeout" if code == "tool_timeout" else "failed"), (
        retryable if isinstance(retryable, bool) else None
    )


def _mcp_result_trace_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    structured = payload.get("structuredContent", payload.get("structured_content"))
    error = structured.get("error") if isinstance(structured, dict) else None
    trace_id = error.get("traceId") if isinstance(error, dict) else None
    return trace_id if isinstance(trace_id, str) else None


def _current_otel_trace_id() -> str | None:
    try:
        from opentelemetry import trace

        context = trace.get_current_span().get_span_context()
        if context.is_valid and context.trace_id:
            return f"{context.trace_id:032x}"
    except (ImportError, RuntimeError):
        pass
    return None


def _error_fields(error: BaseException | None) -> dict[str, Any] | None:
    if error is None:
        return None
    return {
        "type": type(error).__name__,
        "message": str(error),
        "retryable": False,
        "errorTraceId": uuid.uuid4().hex,
        "traceback": "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        ),
    }


def _emit_route_rejection(
    audit_sink: StructuredLogs,
    fatal_reporter: FatalShutdownReporter,
    event: str,
    *,
    request: Request,
    payload: dict[str, Any] | None = None,
) -> None:
    try:
        audit_sink.emit(
            "audit",
            event,
            level="WARNING",
            outcome="rejected",
            payload=payload,
            **_transport_fields(request.scope, _scope_headers(request.scope)),
        )
    except StructuredLogError as error:
        fatal_reporter.logging_failure(error)
        raise


def _required_path(name: str) -> Path:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    path = Path(value)
    if not path.is_absolute():
        raise RuntimeError(f"{name} must be an absolute path")
    return path


def _required_file(name: str) -> Path:
    path = _required_path(name)
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"{name} must name a regular, non-symlink file")
    return path


def _required_directory(name: str) -> Path:
    path = _required_path(name)
    if not path.is_dir() or path.is_symlink():
        raise RuntimeError(f"{name} must name a directory, not a symlink")
    return path


def _json_string_list(name: str, *, required: bool = True) -> list[str]:
    raw = os.environ.get(name)
    if raw is None and not required:
        return []
    if raw is None:
        raise RuntimeError(f"{name} is required")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{name} must be a JSON string array: {error}") from error
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RuntimeError(f"{name} must be a JSON string array")
    return value


def _integer_setting(name: str, *, minimum: int) -> int:
    try:
        value = int(os.environ[name])
    except (KeyError, ValueError) as error:
        raise RuntimeError(f"{name} must be an integer") from error
    if value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    return value


def _float_setting(name: str, *, minimum: float) -> float:
    try:
        value = float(os.environ[name])
    except (KeyError, ValueError) as error:
        raise RuntimeError(f"{name} must be a number") from error
    if not math.isfinite(value) or value < minimum:
        raise RuntimeError(f"{name} must be a finite number of at least {minimum}")
    return value


def _host_without_port(value: str) -> str:
    if value.startswith("["):
        closing = value.find("]")
        return value[1:closing] if closing > 0 else ""
    if value.count(":") == 1:
        return value.rsplit(":", 1)[0]
    return value


def _sanitized_stack(error: BaseException) -> list[str]:
    frames = traceback.extract_tb(error.__traceback__)
    return [
        f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}"
        for frame in frames[-12:]
    ]


def _find_structured_tool_error(
    error: BaseException,
) -> (
    ToolframeError
    | idempotency.IdempotencyError
    | ArtifactError
    | database_sdk.DatabaseError
    | None
):
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(
            current,
            (
                ToolframeError,
                idempotency.IdempotencyError,
                ArtifactError,
                database_sdk.DatabaseError,
            ),
        ):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


def _is_streamlit_control_exception(error: BaseException) -> bool:
    return any(
        base.__name__ == "ScriptControlException"
        and base.__module__.startswith("streamlit.")
        for base in type(error).__mro__
    )


def _dashboard_error_location(
    error: BaseException,
    dashboard_dir: Path,
) -> tuple[str, str | None, str | None]:
    dashboard_root = dashboard_dir.resolve(strict=False)
    phase_value = getattr(error, "phase", None)
    phase = str(phase_value) if phase_value else "script-execution"
    candidate: str | None = None
    explicit_path = getattr(error, "path", None)
    if explicit_path is not None:
        candidate = str(explicit_path)
    elif isinstance(error, SyntaxError) and error.filename:
        candidate = error.filename
        phase = "compile"
    else:
        frames = traceback.extract_tb(error.__traceback__)
        for frame in reversed(frames):
            frame_path = Path(frame.filename)
            try:
                frame_path.resolve(strict=False).relative_to(dashboard_root)
            except ValueError:
                continue
            candidate = str(frame_path)
            break

    widget_value = getattr(error, "widget", None)
    widget = str(widget_value) if widget_value else None
    if candidate is not None:
        candidate_path = Path(candidate)
        try:
            relative = candidate_path.resolve(strict=False).relative_to(dashboard_root)
        except ValueError:
            relative = None
        if (
            relative is not None
            and len(relative.parts) == 2
            and relative.parts[0] == "widgets"
        ):
            widget = relative.stem
            if phase == "script-execution":
                phase = "widget-execution"
        elif relative == Path("app.py") and phase == "script-execution":
            phase = "app-execution"
    return phase, candidate, widget


def _safe_single_line(value: str, *, limit: int = 1024) -> str:
    normalized = " ".join(str(value).splitlines())
    normalized = "".join(
        character if character.isprintable() else " " for character in normalized
    )
    normalized = " ".join(normalized.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)] + "…"
