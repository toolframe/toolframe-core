"""Fail-closed validation for Toolframe's editable Python source contract."""

from __future__ import annotations

import ast
import keyword
import os
import re
import stat
from pathlib import Path

_DOMAIN_NAME = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")
_DOMAIN_DIRECTORIES = ("llms", "model", "tools", "ui", "migrations")
_PYTHON_SOURCE_DIRECTORIES = ("model", "tools", "ui")
_INTERNAL_DOMAIN_MODULES = frozenset(_DOMAIN_DIRECTORIES)


class SourceArchitectureError(RuntimeError):
    """Identify one invalid source file or forbidden domain dependency."""


def validate_source_architecture(app_dir: Path, domains_dir: Path) -> None:
    """Compile public Python sources and enforce domain public-API imports.

    Domain-owned code may import freely within its own domain. Other domains
    may be reached only through named members of their root public API. The
    dashboard is compiled here but its composition choices stay unrestricted.
    """

    app_root = _real_directory(Path(app_dir), "app directory")
    domains_root = _real_directory(Path(domains_dir), "domains directory")
    domain_roots = _discover_domains(domains_root)
    domain_names = frozenset(domain_roots)

    dashboard = _real_file(app_root / "dashboard.py", "app dashboard")
    _validate_python_source(
        dashboard,
        current_domain=None,
        current_module=None,
        domains=domain_names,
        enforce_domain_imports=False,
    )
    plugin_catalog = app_root / "plugins.py"
    if plugin_catalog.exists() or plugin_catalog.is_symlink():
        _validate_python_source(
            _real_file(plugin_catalog, "app plugin catalog"),
            current_domain=None,
            current_module=None,
            domains=domain_names,
            enforce_domain_imports=False,
        )

    for domain, root in domain_roots.items():
        sources = [root / "__init__.py"]
        for child in _PYTHON_SOURCE_DIRECTORIES:
            sources.extend(
                _public_python_sources(
                    root / child,
                    recursive=child == "model",
                )
            )
        for source in sources:
            _validate_python_source(
                source,
                current_domain=domain,
                current_module=_domain_module(source, domains_root),
                domains=domain_names,
                enforce_domain_imports=True,
            )


def _discover_domains(root: Path) -> dict[str, Path]:
    try:
        entries = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError as error:
        raise SourceArchitectureError(
            f"could not read domains directory {root}: {error}"
        ) from error

    domains: dict[str, Path] = {}
    for candidate in entries:
        name = candidate.name
        if name.startswith((".", "_")):
            continue
        if not _DOMAIN_NAME.fullmatch(name) or keyword.iskeyword(name):
            raise SourceArchitectureError(
                f"domain name must be a lower_snake_case Python identifier: {candidate}"
            )
        domain = _real_directory(candidate, f"domain {name}")
        _real_file(domain / "__init__.py", f"domain {name} public API")
        for child in _DOMAIN_DIRECTORIES:
            _real_directory(domain / child, f"domain {name} {child}")
        domains[name] = domain

    if not domains:
        raise SourceArchitectureError(
            f"domains directory must contain at least one public business domain: {root}"
        )
    return domains


def _public_python_sources(root: Path, *, recursive: bool) -> list[Path]:
    sources: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(directory.iterdir(), key=lambda path: path.name)
        except OSError as error:
            raise SourceArchitectureError(
                f"could not read Python source directory {directory}: {error}"
            ) from error
        for candidate in entries:
            name = candidate.name
            if name.startswith(".") or (name.startswith("_") and name != "__init__.py"):
                continue
            try:
                metadata = candidate.lstat()
            except OSError as error:
                raise SourceArchitectureError(
                    f"could not inspect Python source path {candidate}: {error}"
                ) from error
            if stat.S_ISLNK(metadata.st_mode) and (
                recursive or candidate.suffix == ".py"
            ):
                raise SourceArchitectureError(
                    f"public Python source path must not be a symlink: {candidate}"
                )
            if recursive and stat.S_ISDIR(metadata.st_mode):
                pending.append(candidate)
            elif candidate.suffix == ".py":
                if not stat.S_ISREG(metadata.st_mode):
                    raise SourceArchitectureError(
                        f"public Python source must be a regular file: {candidate}"
                    )
                sources.append(candidate)
    return sorted(sources)


def _validate_python_source(
    path: Path,
    *,
    current_domain: str | None,
    current_module: tuple[str, ...] | None,
    domains: frozenset[str],
    enforce_domain_imports: bool,
) -> None:
    try:
        source = path.read_bytes()
        tree = compile(
            source,
            os.fspath(path),
            "exec",
            flags=ast.PyCF_ONLY_AST,
            dont_inherit=True,
        )
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise SourceArchitectureError(
            f"public Python source failed compile validation: {path}: "
            f"{type(error).__name__}: {error}"
        ) from error

    if not enforce_domain_imports:
        return

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _validate_import(
                    alias.name,
                    imported_names=None,
                    path=path,
                    line=node.lineno,
                    current_domain=current_domain,
                    domains=domains,
                )
        elif isinstance(node, ast.ImportFrom):
            module = _resolved_import_from(node, current_module, path)
            if module is None:
                continue
            _validate_import(
                module,
                imported_names=tuple(alias.name for alias in node.names),
                path=path,
                line=node.lineno,
                current_domain=current_domain,
                domains=domains,
            )


def _domain_module(path: Path, domains_root: Path) -> tuple[str, ...]:
    relative = path.relative_to(domains_root)
    parts = list(relative.parts)
    filename = parts.pop()
    if filename != "__init__.py":
        parts.append(Path(filename).stem)
    return ("domains", *parts)


def _resolved_import_from(
    node: ast.ImportFrom,
    current_module: tuple[str, ...] | None,
    path: Path,
) -> str | None:
    if node.level == 0:
        return node.module
    if current_module is None:
        raise SourceArchitectureError(
            f"{path}:{node.lineno}: app/dashboard.py cannot use relative imports"
        )

    package = current_module if path.name == "__init__.py" else current_module[:-1]
    parent_count = node.level - 1
    if parent_count >= len(package):
        raise SourceArchitectureError(
            f"{path}:{node.lineno}: relative import escapes the domains package"
        )
    resolved = package[: len(package) - parent_count]
    if node.module:
        resolved = (*resolved, *node.module.split("."))
    return ".".join(resolved)


def _validate_import(
    module: str,
    *,
    imported_names: tuple[str, ...] | None,
    path: Path,
    line: int,
    current_domain: str | None,
    domains: frozenset[str],
) -> None:
    parts = module.split(".")
    if parts[0] != "domains":
        return
    if len(parts) < 2 or parts[1] not in domains:
        _raise_import_error(
            path,
            line,
            module,
            "the imported business domain does not exist",
        )

    target_domain = parts[1]
    if target_domain == current_domain:
        return

    if imported_names is None:
        _raise_import_error(
            path,
            line,
            module,
            "cross-domain imports must name public API members",
        )
    if len(parts) != 2:
        _raise_import_error(
            path,
            line,
            module,
            "deep cross-domain imports are forbidden",
        )
    if any(
        name == "*" or name.startswith("_") or name in _INTERNAL_DOMAIN_MODULES
        for name in imported_names
    ):
        _raise_import_error(
            path,
            line,
            module,
            "cross-domain imports must select explicit business service contracts or lease-owning functions",
        )


def _raise_import_error(path: Path, line: int, module: str, reason: str) -> None:
    raise SourceArchitectureError(
        f"{path}:{line}: invalid import from {module!r}: {reason}; use "
        "`from domains.<domain> import <public_business_api>` only when needed"
    )


def _real_directory(path: Path, label: str) -> Path:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise SourceArchitectureError(
            f"could not inspect {label} {path}: {error}"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise SourceArchitectureError(
            f"{label} must be a real directory, not a symlink: {path}"
        )
    return path.resolve(strict=True)


def _real_file(path: Path, label: str) -> Path:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise SourceArchitectureError(
            f"could not inspect {label} {path}: {error}"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SourceArchitectureError(
            f"{label} must be a regular file, not a symlink: {path}"
        )
    return path.resolve(strict=True)
