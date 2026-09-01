"""Private runtime support for the lite-mcp FastAPI/FastMCP server."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import logging
import math
import os
import sqlite3
import sys
import traceback
import types
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastmcp import FastMCP
from fastmcp.decorators import get_fastmcp_meta
from fastmcp.server.middleware import Middleware
from fastmcp.tools import Tool, ToolResult
from fastmcp.tools.function_tool import ToolMeta
from fastmcp.utilities.lifespan import combine_lifespans


LOGGER = logging.getLogger("lite_mcp.runtime")
READY_SENTINEL = "LITE_MCP_READY"
DATABASE_PREVIEW_ROWS = 25


class LiteMcpError(Exception):
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

    def __init__(self, concurrency: int, timeout_seconds: float, result_limit: int) -> None:
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
        except BaseException as error:  # Keep tool failures from terminating the server.
            if isinstance(error, anyio.get_cancelled_exc_class()):
                raise
            lite_error = _find_lite_mcp_error(error)
            if lite_error is not None:
                LOGGER.warning(
                    "tool reported LiteMcpError code=%s retryable=%s",
                    lite_error.code,
                    lite_error.retryable,
                    exc_info=True,
                )
                return error_result(
                    lite_error.code,
                    lite_error.message,
                    retryable=lite_error.retryable,
                    details=lite_error.details,
                    exception=lite_error,
                )
            LOGGER.exception("unexpected tool invocation failure")
            exception_name = type(error).__name__
            validation = exception_name == "ValidationError"
            not_found = exception_name == "NotFoundError"
            if validation:
                code = "invalid_arguments"
                message = "The tool arguments did not satisfy its input schema."
                recovery_hint = "Correct the arguments using the published input schema."
            elif not_found:
                code = "tool_not_found"
                message = "The requested tool is not present in the startup catalog."
                recovery_hint = "List tools again and use a tool loaded at process startup."
            else:
                code = "internal_error"
                message = "The tool encountered an unexpected server error."
                recovery_hint = (
                    "Use the traceId to inspect the server log before deciding whether to retry."
                )
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


class HostOriginMiddleware:
    """Validate Host and browser Origin headers for every HTTP route."""

    def __init__(self, app: Any, allowed_hosts: list[str], allowed_origins: list[str]) -> None:
        self.app = app
        self.allowed_hosts = {host.casefold() for host in allowed_hosts}
        self.allowed_origins = {origin.casefold() for origin in allowed_origins}

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").casefold(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        host = _host_without_port(headers.get("host", "")).casefold()
        if not host or host not in self.allowed_hosts:
            await PlainTextResponse("Unrecognized Host header", status_code=421)(scope, receive, send)
            return

        origin = headers.get("origin")
        if origin is not None and origin.casefold() not in self.allowed_origins:
            await PlainTextResponse("Origin is not allowed", status_code=403)(scope, receive, send)
            return

        await self.app(scope, receive, send)


def error_result(
    code: str,
    message: str,
    *,
    retryable: bool,
    details: dict[str, Any] | None = None,
    exception: BaseException | None = None,
) -> ToolResult:
    trace_id = uuid.uuid4().hex
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


def load_tools(manifest_path: Path) -> tuple[list[Tool], list[dict[str, Any]], list[str]]:
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
                raise ToolLoadError(path, "path", "tool path became a symlink after discovery")
            path = path.resolve(strict=True)
            if path.parent != tools_dir or not path.is_file():
                raise ToolLoadError(
                    path,
                    "path",
                    "tool must be a direct, regular, non-symlink child of toolsDir",
                )
            if entry.get("sourceFilename") != path.name:
                raise ToolLoadError(path, "manifest", "source filename does not match path")

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
                raise ToolLoadError(path, "identity", f"duplicate tool name {expected_name!r}")
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
                raise ToolLoadError(path, "schema", "tool must have a non-empty docstring or description")

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
            raise ToolLoadError(path, "import", f"{type(error).__name__}: {error}") from error

    return loaded, runtime_details, sorted(dependencies)


def build_application() -> FastAPI:
    if sys.version_info[:2] != (3, 14) or sys.version_info.releaselevel != "final":
        raise RuntimeError(
            "lite-mcp requires a final Python 3.14 release; "
            f"found {sys.version.split()[0]} ({sys.version_info.releaselevel})"
        )
    manifest_path = _required_path("LITE_MCP_MANIFEST")
    database_path = _required_path("LITE_MCP_DB")
    dashboard_path = _required_path("LITE_MCP_DASHBOARD")
    allowed_hosts = _json_string_list("LITE_MCP_ALLOWED_HOSTS")
    allowed_origins = _json_string_list("LITE_MCP_ALLOWED_ORIGINS")
    concurrency = _integer_setting("LITE_MCP_MAX_CONCURRENCY", minimum=0)
    timeout_seconds = _float_setting("LITE_MCP_TOOL_TIMEOUT_SECONDS", minimum=0)
    result_limit = _integer_setting("LITE_MCP_RESULT_LIMIT_BYTES", minimum=1)
    ready_token = os.environ.get("LITE_MCP_READY_TOKEN", "").strip()
    if not ready_token:
        raise RuntimeError("LITE_MCP_READY_TOKEN is required")

    tools, tool_details, tool_dependencies = load_tools(manifest_path)
    mcp = FastMCP(
        "lite-mcp",
        version=os.environ.get("LITE_MCP_VERSION", "0.0.0"),
        strict_input_validation=True,
    )
    for tool_object in tools:
        mcp.add_tool(tool_object)
    mcp.add_middleware(RuntimeGuardMiddleware(concurrency, timeout_seconds, result_limit))

    mcp_app = mcp.http_app(
        path="/mcp",
        transport="streamable-http",
        stateless_http=False,
        host_origin_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )

    @asynccontextmanager
    async def readiness_lifespan(_: FastAPI):
        thread_limiter = anyio.to_thread.current_default_thread_limiter()
        previous_thread_tokens = thread_limiter.total_tokens
        thread_limiter.total_tokens = math.inf if concurrency == 0 else concurrency
        try:
            print(f"{READY_SENTINEL} {ready_token}", flush=True)
            yield
        finally:
            thread_limiter.total_tokens = previous_thread_tokens

    app = FastAPI(
        title="lite-mcp",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=combine_lifespans(mcp_app.lifespan, readiness_lifespan),
    )
    app.add_middleware(
        HostOriginMiddleware,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )

    dashboard = dashboard_path.read_text(encoding="utf-8")
    dependency_summary = _json_string_list("LITE_MCP_DEPENDENCIES_JSON", required=False)
    if not dependency_summary:
        dependency_summary = tool_dependencies

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(dashboard)

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
        return {
            "pythonVersion": sys.version.split()[0],
            "tools": tool_details,
            "dependencies": dependency_summary,
            "startupOnly": True,
            "maxConcurrency": concurrency,
            "toolTimeoutSeconds": timeout_seconds,
            "resultLimitBytes": result_limit,
        }

    # Mount last as a catch-all so FastMCP owns the exact /mcp path without a
    # POST redirect to /mcp/. The explicit FastAPI routes above keep precedence.
    app.mount("/", mcp_app)
    return app


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:
        raise ToolLoadError(path, "manifest", f"{type(error).__name__}: {error}") from error
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ToolLoadError(path, "manifest", "unsupported or missing manifest version")
    if manifest.get("pythonVersion") != "3.14.0":
        raise ToolLoadError(path, "manifest", "runtime requires Python 3.14.0")
    if not isinstance(manifest.get("tools"), list):
        raise ToolLoadError(path, "manifest", "tools must be an array")
    return manifest


def _import_tool(path: Path, name: str, source_hash: str) -> types.ModuleType:
    module_name = f"lite_mcp_user_{name}_{source_hash[:16]}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ToolLoadError(path, "import", "could not create a Python module specification")
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
            cursor = connection.execute(f"SELECT * FROM {identifier} LIMIT ?", (DATABASE_PREVIEW_ROWS + 1,))
            rows = cursor.fetchall()
            tables.append(
                {
                    "name": name,
                    "columns": [description[0] for description in cursor.description or []],
                    "rows": [[_json_value(value) for value in row] for row in rows[:DATABASE_PREVIEW_ROWS]],
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


def _required_path(name: str) -> Path:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    path = Path(value)
    if not path.is_absolute():
        raise RuntimeError(f"{name} must be an absolute path")
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


def _find_lite_mcp_error(error: BaseException) -> LiteMcpError | None:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, LiteMcpError):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None
