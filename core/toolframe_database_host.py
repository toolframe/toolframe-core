"""Read-only preflight copies and bounded database diagnostics for the host."""

from __future__ import annotations

import hashlib
import math
import shutil
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from toolframe_database_service import create_provider
from toolframe_migrations import MigrationError, _table_names


def _fingerprint(path):
    result = {}
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if not candidate.exists():
            continue
        if not candidate.is_file() or candidate.is_symlink():
            raise MigrationError(
                "invalid_snapshot_source", "Database snapshots require regular files."
            )
        digest = hashlib.sha256()
        with candidate.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        result[suffix] = (candidate.stat().st_size, digest.hexdigest())
    return result


@contextmanager
def database_check(settings):
    """Never open the configured SQLite file; PostgreSQL checks remain read-only."""
    provider = create_provider(settings)
    try:
        if provider.dialect != "sqlite":
            yield provider, "history-and-plan", "read-only-target"
            return
        provider.close()
        with tempfile.TemporaryDirectory(
            prefix="toolframe-database-check-"
        ) as temporary:
            destination = Path(temporary) / "data.db"
            before = _fingerprint(settings.sqlite_path)
            if before and "" not in before:
                raise MigrationError(
                    "invalid_snapshot_source",
                    "SQLite sidecar files have no database file.",
                )
            if "" in before:
                for suffix in before:
                    shutil.copyfile(
                        Path(str(settings.sqlite_path) + suffix),
                        Path(str(destination) + suffix),
                    )
                after = _fingerprint(settings.sqlite_path)
                if before != after or _fingerprint(destination) != before:
                    raise MigrationError(
                        "snapshot_changed",
                        "The SQLite database changed while being copied; retry check.",
                        retryable=True,
                    )
            provider = create_provider(
                settings.model_copy(update={"sqlite_path": destination})
            )
            try:
                yield (
                    provider,
                    "schema-trial",
                    "filesystem-snapshot" if "" in before else "empty-snapshot",
                )
            finally:
                provider.close()
    finally:
        provider.close()


def database_snapshot(provider, *, limit=25):
    """Preview business data using the selected provider's read bounds."""
    with provider.connection(read_only=True) as connection:
        names = sorted(
            name
            for name in _table_names(connection, provider.dialect)
            if not name.startswith(("_toolframe_", "sqlite_", "pg_"))
        )
    limit = min(limit, provider.settings.max_rows)
    tables = []
    for name in names:
        identifier = '"' + name.replace('"', '""') + '"'
        frame = provider.query(
            f"SELECT * FROM {identifier} LIMIT :limit", {"limit": limit}, max_rows=limit
        )
        extra = provider.query(
            f"SELECT EXISTS(SELECT 1 FROM {identifier} LIMIT 1 OFFSET :limit) AS extra",
            {"limit": limit},
        )
        tables.append(
            {
                "name": name,
                "columns": list(frame.columns),
                "rows": [
                    [_json_value(value) for value in row]
                    for row in frame.itertuples(index=False, name=None)
                ],
                "truncated": bool(extra.iloc[0, 0]),
            }
        )
    info = provider.public_info()
    return {
        "databasePath": info["target"],
        "database": info,
        "generatedAt": datetime.now(UTC).isoformat(),
        "tables": tables,
    }


def _json_value(value):
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<blob: {len(value)} bytes>"
    return str(value)
