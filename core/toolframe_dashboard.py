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
import os
import re
import stat
import sys
import threading
import uuid
from collections.abc import Iterable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any

from fastapi_cordis import PluginManager, PluginUnavailable
from toolframe_architecture import validate_source_architecture
from toolframe_artifacts import ArtifactStore
from toolframe_database import (
    DashboardDatabase,
    DashboardDatabaseError,
    ExecuteResult,
    _dashboard_request_metadata,
)
from toolframe_idempotency import (
    IdempotencyError,
    IdempotencyResult,
    IdempotencyTransaction,
)

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
_DEFAULT_WIDGET_ORDER = 1_000
_DASHBOARD_SESSION_STATE_KEY = "_toolframe_dashboard_session_id"
_DOMAIN_NAME = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")
_WIDGET_NAME = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")
_DOMAIN_DIRECTORIES = ("llms", "model", "tools", "ui", "migrations")


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
class DashboardContext:
    """Stable capabilities supplied to one dashboard widget."""

    db: DashboardDatabase
    workspace: Path
    logger: logging.Logger
    widget_id: str
    runtime: Mapping[str, Any]
    artifacts: ArtifactStore | None = None
    plugins: PluginManager | None = None


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
    plugins: PluginManager | None = None,
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
        plugins=plugins,
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
        try:
            lease = (
                base_context.plugins.lease(f"domain.{widget.domain}")
                if base_context.plugins is not None
                else nullcontext()
            )
            with lease:
                widget.module.render(widget_context)
        except PluginUnavailable as error:
            import streamlit as st

            st.info(
                f"{widget.title} is unavailable while plugin {error.name} is stopped."
            )
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
