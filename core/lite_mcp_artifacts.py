"""Managed local artifact storage shared by lite-mcp tools and dashboards.

Artifacts are trusted local data, not a security boundary.  This module keeps
the supported path inside ``workspace/artifacts`` and makes traversal,
symlink, collision, size, and integrity failures explicit.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import mimetypes
import os
import re
import stat
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, BinaryIO

__all__ = [
    "DEFAULT_MAX_ARTIFACT_BYTES",
    "Artifact",
    "ArtifactError",
    "ArtifactExistsError",
    "ArtifactIntegrityError",
    "ArtifactLimitError",
    "ArtifactMetadataError",
    "ArtifactNotFoundError",
    "ArtifactPathError",
    "ArtifactStorageError",
    "ArtifactStore",
    "configure_artifact_store",
    "get_artifact_store",
]


DEFAULT_MAX_ARTIFACT_BYTES = 25 * 1024 * 1024
DEFAULT_LIST_LIMIT = 1_000
MAX_LIST_LIMIT = 10_000
MAX_PATH_BYTES = 1_024
MAX_MIME_TYPE_BYTES = 255
MAX_METADATA_BYTES = 4 * 1024
_METADATA_DIRECTORY = ".lite-mcp"
_METADATA_SCHEMA_VERSION = 1
_COPY_CHUNK_BYTES = 64 * 1024
_ARTIFACT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_MIME_TYPE_PATTERN = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")
_LOGGER = logging.getLogger("lite_mcp.artifacts")


class ArtifactError(RuntimeError):
    """Base class for structured artifact failures."""

    code = "artifact_error"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.message = message
        self.details = MappingProxyType(dict(details or {}))
        super().__init__(message)


class ArtifactPathError(ArtifactError):
    """The supplied artifact path or ID is invalid."""

    code = "invalid_artifact_path"


class ArtifactNotFoundError(ArtifactError):
    """No managed artifact matches the requested path or ID."""

    code = "artifact_not_found"


class ArtifactExistsError(ArtifactError):
    """The create-once artifact path is already occupied."""

    code = "artifact_exists"


class ArtifactLimitError(ArtifactError):
    """An artifact input exceeds a configured deterministic limit."""

    code = "artifact_limit_exceeded"


class ArtifactMetadataError(ArtifactError):
    """Caller-supplied artifact metadata is invalid."""

    code = "invalid_artifact_metadata"


class ArtifactIntegrityError(ArtifactError):
    """Stored content and its managed metadata no longer agree."""

    code = "artifact_integrity_error"


class ArtifactStorageError(ArtifactError):
    """The local filesystem could not complete an artifact operation."""

    code = "artifact_storage_error"


@dataclass(frozen=True, slots=True)
class Artifact:
    """Stable metadata for one managed artifact."""

    artifact_id: str
    path: str
    mime_type: str
    byte_size: int
    sha256: str
    created_at: datetime

    def to_reference(self) -> dict[str, Any]:
        """Return a JSON-safe reference suitable for an MCP tool result."""

        return {
            "artifactId": self.artifact_id,
            "path": self.path,
            "mimeType": self.mime_type,
            "byteSize": self.byte_size,
            "sha256": self.sha256,
            "createdAt": _format_datetime(self.created_at),
        }


class ArtifactStore:
    """Create-once, integrity-checked storage under one workspace.

    Relative paths use POSIX separators on every platform.  Managed writes are
    published atomically without replacement.  Direct filesystem mutation is
    unsupported and is detected when metadata or content no longer agrees.
    """

    def __init__(
        self,
        workspace: Path,
        *,
        max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
        audit_sink: Any | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if (
            isinstance(max_artifact_bytes, bool)
            or not isinstance(max_artifact_bytes, int)
            or max_artifact_bytes <= 0
        ):
            raise ValueError("max_artifact_bytes must be a positive integer")
        workspace_candidate = Path(workspace).expanduser().absolute()
        if workspace_candidate.is_symlink():
            raise ArtifactStorageError(
                "The workspace directory must not be a symlink.",
                details={"workspace": os.fspath(workspace_candidate)},
            )
        try:
            workspace_root = workspace_candidate.resolve(strict=True)
        except OSError as error:
            raise ArtifactStorageError(
                "The configured workspace is unavailable.",
                details={"workspace": os.fspath(workspace_candidate)},
            ) from error
        if not workspace_root.is_dir():
            raise ArtifactStorageError(
                "The configured workspace is not a directory.",
                details={"workspace": os.fspath(workspace_root)},
            )

        self.workspace = workspace_root
        self.root = workspace_root / "artifacts"
        self._metadata_root = self.root / _METADATA_DIRECTORY
        self.max_artifact_bytes = max_artifact_bytes
        self.audit_sink = audit_sink
        self.logger = logger or _LOGGER
        self._lock = threading.RLock()
        self._prepare_directories()

    def write(
        self,
        path: str,
        data: bytes | bytearray | memoryview | BinaryIO,
        *,
        mime_type: str | None = None,
    ) -> Artifact:
        """Create one artifact from bytes or a binary stream."""

        operation_id = uuid.uuid4().hex
        started_at = time.monotonic()
        try:
            normalized, parts = _normalize_path(path)
            resolved_mime_type = _normalize_mime_type(mime_type, normalized)
        except ArtifactError as error:
            self._emit_audit(
                "artifact_write_rejected",
                level="WARNING",
                outcome="rejected",
                payload={"code": error.code},
                operationId=operation_id,
                method="write",
                retryable=error.retryable,
                durationMs=_duration_ms(started_at),
            )
            raise
        self._emit_audit(
            "artifact_write_started",
            outcome="started",
            payload={"path": normalized, "mimeType": resolved_mime_type},
            operationId=operation_id,
            method="write",
        )
        try:
            with self._lock:
                artifact = self._write_locked(
                    normalized, parts, data, resolved_mime_type
                )
        except ArtifactError as error:
            self._emit_audit(
                "artifact_write_rejected",
                level="WARNING",
                outcome="rejected",
                payload={"path": normalized, "code": error.code},
                operationId=operation_id,
                method="write",
                retryable=error.retryable,
                durationMs=_duration_ms(started_at),
            )
            raise
        self._emit_audit(
            "artifact_write_completed",
            outcome="success",
            payload=artifact.to_reference(),
            operationId=operation_id,
            method="write",
            durationMs=_duration_ms(started_at),
        )
        return artifact

    def write_text(
        self,
        path: str,
        text: str,
        *,
        mime_type: str = "text/plain",
        encoding: str = "utf-8",
    ) -> Artifact:
        """Encode and create one text artifact."""

        if not isinstance(text, str):
            raise TypeError("text must be a string")
        try:
            payload = text.encode(encoding)
        except (LookupError, UnicodeError) as error:
            raise ArtifactStorageError(
                "The text artifact could not be encoded.",
                details={"encoding": encoding},
            ) from error
        return self.write(path, payload, mime_type=mime_type)

    def read(self, path: str) -> bytes:
        """Read and verify an artifact by its relative path."""

        normalized, _ = _normalize_path(path)
        with self._lock:
            artifact = self._find_by_path_locked(normalized)
            return self._read_locked(artifact)

    def read_by_id(self, artifact_id: str) -> bytes:
        """Read and verify an artifact by its stable ID."""

        with self._lock:
            artifact = self._find_by_id_locked(_normalize_artifact_id(artifact_id))
            return self._read_locked(artifact)

    def get(self, path: str) -> Artifact:
        """Return metadata for an artifact path without loading its content."""

        normalized, _ = _normalize_path(path)
        with self._lock:
            artifact = self._find_by_path_locked(normalized)
            self._validate_content_stat(artifact)
            return artifact

    def get_by_id(self, artifact_id: str) -> Artifact:
        """Return metadata for a stable artifact ID."""

        with self._lock:
            artifact = self._find_by_id_locked(_normalize_artifact_id(artifact_id))
            self._validate_content_stat(artifact)
            return artifact

    def list(
        self,
        *,
        prefix: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> tuple[Artifact, ...]:
        """List metadata in path order, optionally under a path prefix."""

        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_LIST_LIMIT
        ):
            raise ArtifactLimitError(
                f"limit must be an integer from 1 through {MAX_LIST_LIMIT}.",
                details={"limit": limit, "maxLimit": MAX_LIST_LIMIT},
            )
        normalized_prefix = None
        if prefix is not None:
            normalized_prefix, _ = _normalize_path(prefix)
        with self._lock:
            artifacts = sorted(
                self._load_all_metadata_locked(), key=lambda item: item.path
            )
            if normalized_prefix is not None:
                prefix_start = f"{normalized_prefix}/"
                artifacts = [
                    artifact
                    for artifact in artifacts
                    if artifact.path == normalized_prefix
                    or artifact.path.startswith(prefix_start)
                ]
            if len(artifacts) > limit:
                raise ArtifactLimitError(
                    "The artifact listing exceeds the requested item limit.",
                    details={"matchedItems": len(artifacts), "limit": limit},
                )
            for artifact in artifacts:
                self._validate_content_stat(artifact)
            return tuple(artifacts)

    def _prepare_directories(self) -> None:
        for directory, label in (
            (self.root, "artifact root"),
            (self._metadata_root, "artifact metadata directory"),
        ):
            try:
                if directory.is_symlink():
                    raise ArtifactStorageError(
                        f"The {label} must not be a symlink.",
                        details={"path": os.fspath(directory)},
                    )
                directory.mkdir(mode=0o700, exist_ok=True)
                if directory.is_symlink() or not directory.is_dir():
                    raise ArtifactStorageError(
                        f"The {label} is not a regular directory.",
                        details={"path": os.fspath(directory)},
                    )
            except ArtifactError:
                raise
            except OSError as error:
                raise ArtifactStorageError(
                    f"The {label} could not be prepared.",
                    details={"path": os.fspath(directory)},
                ) from error

    def _write_locked(
        self,
        normalized: str,
        parts: tuple[str, ...],
        data: bytes | bytearray | memoryview | BinaryIO,
        mime_type: str,
    ) -> Artifact:
        existing = self._find_optional_by_path_locked(normalized)
        if existing is not None:
            raise ArtifactExistsError(
                "A managed artifact already uses this path.",
                details={"path": normalized, "artifactId": existing.artifact_id},
            )

        parent = self._ensure_parent_directories(parts[:-1])
        target = parent / parts[-1]
        if os.path.lexists(target):
            if target.is_symlink():
                raise ArtifactPathError(
                    "The artifact path resolves through a symlink.",
                    details={"path": normalized},
                )
            raise ArtifactExistsError(
                "The artifact path is already occupied by unmanaged content.",
                details={"path": normalized},
            )

        artifact_id = uuid.uuid4().hex
        metadata_target = self._metadata_root / f"{artifact_id}.json"
        content_temp = parent / f".lite-mcp-{artifact_id}.tmp"
        metadata_temp = self._metadata_root / f".{artifact_id}.tmp"
        content_published = False
        try:
            byte_size, digest = self._write_content_temp(content_temp, data)
            artifact = Artifact(
                artifact_id=artifact_id,
                path=normalized,
                mime_type=mime_type,
                byte_size=byte_size,
                sha256=digest,
                created_at=datetime.now(UTC),
            )
            self._write_metadata_temp(metadata_temp, artifact)
            try:
                os.link(content_temp, target, follow_symlinks=False)
            except FileExistsError as error:
                raise ArtifactExistsError(
                    "The artifact path was claimed by another writer.",
                    details={"path": normalized},
                ) from error
            content_published = True
            os.unlink(content_temp)
            os.link(metadata_temp, metadata_target, follow_symlinks=False)
            os.unlink(metadata_temp)
            _sync_directory(parent)
            _sync_directory(self._metadata_root)
            return artifact
        except ArtifactError:
            raise
        except OSError as error:
            raise ArtifactStorageError(
                "The artifact could not be written atomically.",
                details={"path": normalized},
            ) from error
        finally:
            for temporary in (content_temp, metadata_temp):
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    self.logger.warning(
                        "could not remove artifact temporary %s", temporary
                    )
            if content_published and not metadata_target.exists():
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    self.logger.error(
                        "could not roll back unindexed artifact %s", target
                    )

    def _write_content_temp(
        self,
        temporary: Path,
        data: bytes | bytearray | memoryview | BinaryIO,
    ) -> tuple[int, str]:
        source: BinaryIO
        if isinstance(data, (bytes, bytearray, memoryview)):
            source = io.BytesIO(bytes(data))
        elif callable(getattr(data, "read", None)):
            source = data
        else:
            raise ArtifactMetadataError(
                "Artifact data must be bytes-like or a binary stream."
            )

        digest = hashlib.sha256()
        byte_size = 0
        try:
            with temporary.open("xb", buffering=0) as destination:
                while True:
                    chunk = source.read(_COPY_CHUNK_BYTES)
                    if chunk in (b"", None):
                        break
                    if not isinstance(chunk, (bytes, bytearray, memoryview)):
                        raise ArtifactMetadataError(
                            "Artifact streams must return bytes."
                        )
                    chunk_bytes = bytes(chunk)
                    byte_size += len(chunk_bytes)
                    if byte_size > self.max_artifact_bytes:
                        raise ArtifactLimitError(
                            "The artifact exceeds the configured byte limit.",
                            details={
                                "limitBytes": self.max_artifact_bytes,
                                "observedBytes": byte_size,
                            },
                        )
                    digest.update(chunk_bytes)
                    destination.write(chunk_bytes)
                destination.flush()
                os.fsync(destination.fileno())
        except ArtifactError:
            raise
        except (OSError, ValueError) as error:
            raise ArtifactStorageError(
                "The artifact source could not be copied to local storage."
            ) from error
        return byte_size, digest.hexdigest()

    def _write_metadata_temp(self, temporary: Path, artifact: Artifact) -> None:
        document = {
            "schemaVersion": _METADATA_SCHEMA_VERSION,
            **artifact.to_reference(),
        }
        encoded = (
            json.dumps(
                document,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        with temporary.open("xb", buffering=0) as metadata_file:
            metadata_file.write(encoded)
            metadata_file.flush()
            os.fsync(metadata_file.fileno())

    def _ensure_parent_directories(self, parts: tuple[str, ...]) -> Path:
        current = self.root
        for part in parts:
            current = current / part
            try:
                if os.path.lexists(current):
                    if current.is_symlink() or not current.is_dir():
                        raise ArtifactPathError(
                            "The artifact path contains a symlink or non-directory component.",
                            details={"component": part},
                        )
                else:
                    current.mkdir(mode=0o700)
            except ArtifactError:
                raise
            except FileExistsError:
                if current.is_symlink() or not current.is_dir():
                    raise ArtifactPathError(
                        "The artifact path contains a symlink or non-directory component.",
                        details={"component": part},
                    )
            except OSError as error:
                raise ArtifactStorageError(
                    "An artifact parent directory could not be prepared.",
                    details={"component": part},
                ) from error
        return current

    def _read_locked(self, artifact: Artifact) -> bytes:
        _, parts = _normalize_path(artifact.path)
        target = self._checked_existing_path(parts, artifact.path)
        self._validate_content_stat(artifact, target=target)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target, flags)
            with os.fdopen(descriptor, "rb") as source:
                payload = source.read(self.max_artifact_bytes + 1)
        except FileNotFoundError as error:
            raise ArtifactIntegrityError(
                "Managed artifact content is missing.",
                details={"path": artifact.path, "artifactId": artifact.artifact_id},
            ) from error
        except OSError as error:
            raise ArtifactStorageError(
                "Managed artifact content could not be read.",
                details={"path": artifact.path},
            ) from error
        actual_size = len(payload)
        actual_sha256 = hashlib.sha256(payload).hexdigest()
        if (
            actual_size > self.max_artifact_bytes
            or actual_size != artifact.byte_size
            or actual_sha256 != artifact.sha256
        ):
            raise ArtifactIntegrityError(
                "Managed artifact content does not match its metadata.",
                details={
                    "path": artifact.path,
                    "artifactId": artifact.artifact_id,
                    "expectedBytes": artifact.byte_size,
                    "actualBytes": actual_size,
                    "expectedSha256": artifact.sha256,
                    "actualSha256": actual_sha256,
                },
            )
        return payload

    def _validate_content_stat(
        self, artifact: Artifact, *, target: Path | None = None
    ) -> None:
        if target is None:
            _, parts = _normalize_path(artifact.path)
            target = self._checked_existing_path(parts, artifact.path)
        try:
            metadata = target.stat(follow_symlinks=False)
        except OSError as error:
            raise ArtifactStorageError(
                "Managed artifact content could not be inspected.",
                details={"path": artifact.path},
            ) from error
        if metadata.st_size != artifact.byte_size:
            raise ArtifactIntegrityError(
                "Managed artifact size does not match its metadata.",
                details={
                    "path": artifact.path,
                    "artifactId": artifact.artifact_id,
                    "expectedBytes": artifact.byte_size,
                    "actualBytes": metadata.st_size,
                },
            )

    def _checked_existing_path(self, parts: tuple[str, ...], normalized: str) -> Path:
        current = self.root
        for index, part in enumerate(parts):
            current = current / part
            try:
                metadata = current.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    raise ArtifactPathError(
                        "The artifact path resolves through a symlink.",
                        details={"path": normalized, "component": part},
                    )
                if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
                    raise ArtifactIntegrityError(
                        "Managed artifact content has an invalid parent path.",
                        details={"path": normalized, "component": part},
                    )
                if index == len(parts) - 1 and not stat.S_ISREG(metadata.st_mode):
                    raise ArtifactIntegrityError(
                        "Managed artifact content is not a regular file.",
                        details={"path": normalized},
                    )
            except FileNotFoundError as error:
                raise ArtifactIntegrityError(
                    "Managed artifact content is missing.",
                    details={"path": normalized},
                ) from error
            except OSError as error:
                raise ArtifactStorageError(
                    "The artifact path could not be inspected.",
                    details={"path": normalized},
                ) from error
        return current

    def _find_by_path_locked(self, normalized: str) -> Artifact:
        artifact = self._find_optional_by_path_locked(normalized)
        if artifact is None:
            raise ArtifactNotFoundError(
                "No managed artifact uses this path.", details={"path": normalized}
            )
        return artifact

    def _find_optional_by_path_locked(self, normalized: str) -> Artifact | None:
        matches = [
            artifact
            for artifact in self._load_all_metadata_locked()
            if artifact.path == normalized
        ]
        if len(matches) > 1:
            raise ArtifactIntegrityError(
                "Multiple metadata records claim the same artifact path.",
                details={"path": normalized},
            )
        return matches[0] if matches else None

    def _find_by_id_locked(self, artifact_id: str) -> Artifact:
        metadata_path = self._metadata_root / f"{artifact_id}.json"
        if metadata_path.is_symlink():
            raise ArtifactIntegrityError(
                "Artifact metadata must not be a symlink.",
                details={"artifactId": artifact_id},
            )
        if not metadata_path.is_file():
            raise ArtifactNotFoundError(
                "No managed artifact uses this ID.",
                details={"artifactId": artifact_id},
            )
        return self._load_metadata(metadata_path)

    def _load_all_metadata_locked(self) -> list[Artifact]:
        try:
            paths = sorted(self._metadata_root.iterdir(), key=lambda path: path.name)
        except OSError as error:
            raise ArtifactStorageError(
                "The artifact metadata index could not be listed."
            ) from error
        artifacts: list[Artifact] = []
        for path in paths:
            if path.name.startswith("."):
                continue
            if path.is_symlink():
                raise ArtifactIntegrityError(
                    "Artifact metadata must not be a symlink.",
                    details={"metadataFile": path.name},
                )
            if not path.is_file() or not path.name.endswith(".json"):
                raise ArtifactIntegrityError(
                    "The artifact metadata directory contains an unsupported entry.",
                    details={"metadataFile": path.name},
                )
            artifacts.append(self._load_metadata(path))
        return artifacts

    def _load_metadata(self, path: Path) -> Artifact:
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as metadata_file:
                encoded = metadata_file.read(MAX_METADATA_BYTES + 1)
            if len(encoded) > MAX_METADATA_BYTES:
                raise ValueError("metadata exceeds byte limit")
            document = json.loads(encoded.decode("utf-8"))
            if not isinstance(document, dict):
                raise TypeError("metadata must be an object")
            if document.get("schemaVersion") != _METADATA_SCHEMA_VERSION:
                raise ValueError("unsupported schemaVersion")
            artifact_id = _normalize_artifact_id(document["artifactId"])
            if path.name != f"{artifact_id}.json":
                raise ValueError("metadata filename does not match artifactId")
            normalized, _ = _normalize_path(document["path"])
            mime_type = _normalize_mime_type(document["mimeType"], normalized)
            byte_size = document["byteSize"]
            if (
                isinstance(byte_size, bool)
                or not isinstance(byte_size, int)
                or not 0 <= byte_size <= self.max_artifact_bytes
            ):
                raise ValueError("invalid byteSize")
            sha256 = document["sha256"]
            if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
                raise ValueError("invalid sha256")
            created_at = _parse_datetime(document["createdAt"])
            expected_keys = {
                "schemaVersion",
                "artifactId",
                "path",
                "mimeType",
                "byteSize",
                "sha256",
                "createdAt",
            }
            if set(document) != expected_keys:
                raise ValueError("metadata fields do not match schema")
        except (
            ArtifactError,
            KeyError,
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise ArtifactIntegrityError(
                "Artifact metadata is malformed or unreadable.",
                details={"metadataFile": path.name},
            ) from error
        return Artifact(
            artifact_id=artifact_id,
            path=normalized,
            mime_type=mime_type,
            byte_size=byte_size,
            sha256=sha256,
            created_at=created_at,
        )

    def _emit_audit(
        self,
        event: str,
        *,
        outcome: str,
        payload: Mapping[str, Any],
        level: str = "INFO",
        **fields: Any,
    ) -> None:
        if self.audit_sink is None:
            return
        self.audit_sink.emit(
            "audit",
            event,
            level=level,
            outcome=outcome,
            payload=dict(payload),
            **fields,
        )


def _normalize_path(path: str) -> tuple[str, tuple[str, ...]]:
    if not isinstance(path, str):
        raise ArtifactPathError("Artifact paths must be strings.")
    if not path or path.startswith("/") or "\\" in path or "\0" in path:
        raise ArtifactPathError(
            "Artifact paths must be non-empty relative POSIX paths.",
            details={"path": path},
        )
    try:
        encoded = path.encode("utf-8")
    except UnicodeError as error:
        raise ArtifactPathError("Artifact paths must be valid UTF-8.") from error
    if len(encoded) > MAX_PATH_BYTES:
        raise ArtifactLimitError(
            "The artifact path exceeds the byte limit.",
            details={"pathBytes": len(encoded), "limitBytes": MAX_PATH_BYTES},
        )
    parts = tuple(path.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise ArtifactPathError(
            "Artifact paths must not contain empty, dot, or parent components.",
            details={"path": path},
        )
    if any(
        part == _METADATA_DIRECTORY or part.startswith(".lite-mcp-") for part in parts
    ):
        raise ArtifactPathError(
            "The artifact path uses a reserved lite-mcp name.",
            details={"path": path},
        )
    if any(len(part.encode("utf-8")) > 255 for part in parts):
        raise ArtifactLimitError(
            "An artifact path component exceeds 255 UTF-8 bytes.",
            details={"path": path},
        )
    return "/".join(parts), parts


def _normalize_artifact_id(artifact_id: str) -> str:
    if not isinstance(artifact_id, str) or not _ARTIFACT_ID_PATTERN.fullmatch(
        artifact_id
    ):
        raise ArtifactPathError(
            "Artifact IDs must contain exactly 32 lowercase hexadecimal characters."
        )
    return artifact_id


def _normalize_mime_type(mime_type: str | None, path: str) -> str:
    candidate = mime_type
    if candidate is None:
        candidate = (
            mimetypes.guess_type(path, strict=False)[0] or "application/octet-stream"
        )
    if not isinstance(candidate, str):
        raise ArtifactMetadataError("Artifact MIME types must be strings.")
    candidate = candidate.strip().lower()
    if (
        not candidate
        or len(candidate.encode("utf-8")) > MAX_MIME_TYPE_BYTES
        or not _MIME_TYPE_PATTERN.fullmatch(candidate)
    ):
        raise ArtifactMetadataError(
            "Artifact MIME types must use a valid type/subtype without parameters.",
            details={"mimeType": candidate},
        )
    return candidate


def _format_datetime(value: datetime) -> str:
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _duration_ms(started_at: float) -> float:
    return round((time.monotonic() - started_at) * 1_000, 3)


def _parse_datetime(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("createdAt must be UTC")
    parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("createdAt must be UTC")
    return parsed.astimezone(UTC)


def _sync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


_configuration_lock = threading.RLock()
_configured_store: ArtifactStore | None = None


def configure_artifact_store(
    workspace: Path,
    *,
    max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
    audit_sink: Any | None = None,
    logger: logging.Logger | None = None,
) -> ArtifactStore:
    """Configure and return the process-global store before tool imports."""

    global _configured_store
    store = ArtifactStore(
        workspace,
        max_artifact_bytes=max_artifact_bytes,
        audit_sink=audit_sink,
        logger=logger,
    )
    with _configuration_lock:
        _configured_store = store
    return store


def get_artifact_store() -> ArtifactStore:
    """Return the process-global store available to startup-loaded tools."""

    with _configuration_lock:
        if _configured_store is None:
            raise RuntimeError("lite_mcp_artifacts is not configured")
        return _configured_store
