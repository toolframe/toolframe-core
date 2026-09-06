"""Author namespaced migration drafts without connecting to a database."""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

from toolframe_migrations import MigrationError, discover_domains, read_schema


def scaffold_migration(domains_dir: Path, domain: str, description: str) -> dict:
    """Append one draft revision; leave every previous SQL source unchanged."""
    if (
        not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", domain)
        or domain in {"legacy", "toolframe"}
        or not re.fullmatch(r"[a-z][a-z0-9_]{0,57}", description)
    ):
        raise MigrationError(
            "invalid_scaffold_name",
            "Use lowercase business identifiers and an underscore-separated description.",
        )
    directory = Path(domains_dir) / domain / "migrations"
    for path in (Path(domains_dir), directory.parent, directory):
        if path.is_symlink() or not path.is_dir():
            raise MigrationError(
                "invalid_scaffold_directory",
                "Scaffold requires an existing domain with regular directories.",
            )
    lock = directory / ".scaffold.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise MigrationError(
            "scaffold_locked",
            "Another scaffold owns .scaffold.lock; if its process stopped, inspect unfinished files before removing that lock.",
        ) from None
    created = []
    temporary_manifest = directory / f".schema-{uuid.uuid4().hex}.tmp"
    try:
        os.close(descriptor)
        manifest = directory / "schema.json"
        original = None
        if manifest.exists() or manifest.is_symlink():
            schema = read_schema(manifest)
            if schema.owner != domain:
                raise MigrationError(
                    "invalid_owner", "The schema owner must match its domain."
                )
            original = manifest.read_bytes()
            content = json.loads(original)
            dependencies = [f"{domain}@{schema.target}"]
        else:
            _, legacy = discover_domains(Path(domains_dir))
            owned = sorted(item.ref for item in legacy if item.owner == domain)
            dependencies = owned[-1:]
            content = {"owner": domain, "target": "", "revisions": []}
        serials = [
            int(match[1])
            for item in content["revisions"]
            if (match := re.fullmatch(r"(\d{4})_.+", item["revision"]))
        ]
        number = max(serials, default=0) + 1
        if number > 9999:
            raise MigrationError(
                "scaffold_revision_limit",
                "Choose an explicit revision identifier in schema.json after 9999 scaffolded revisions.",
            )
        revision = f"{number:04}_{description}"
        sources = {}
        for dialect in ("sqlite", "postgresql"):
            parent = directory / dialect
            if parent.is_symlink():
                raise MigrationError(
                    "invalid_scaffold_directory",
                    "Migration variant directories must not be symlinks.",
                )
            parent.mkdir(exist_ok=True)
            path = parent / f"{revision}.sql"
            with path.open("x", encoding="utf-8") as output:
                created.append(path)
                output.write(
                    f"-- {domain}@{revision} ({dialect})\n-- Write transactional SQL and declare its owned tables in schema.json.\n-- This draft intentionally fails check until completed.\n"
                )
                output.flush()
                os.fsync(output.fileno())
            sources[dialect] = path.relative_to(directory).as_posix()
        content["revisions"].append(
            {
                "revision": revision,
                "sql": sources,
                "tables": [],
                "depends": dependencies,
                "reads": [],
            }
        )
        content["target"] = revision
        with temporary_manifest.open("x", encoding="utf-8") as output:
            json.dump(content, output, indent=2, ensure_ascii=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        if (
            manifest.is_symlink()
            or (manifest.read_bytes() if manifest.exists() else None) != original
        ):
            raise MigrationError(
                "scaffold_changed",
                "The schema manifest changed while scaffolding; retry after reviewing the edits.",
            )
        os.replace(temporary_manifest, manifest)
        return {
            "manifest": str(manifest.resolve()),
            "revision": f"{domain}@{revision}",
            "sources": [str(path.resolve()) for path in created],
            "draft": True,
        }
    except BaseException:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise
    finally:
        temporary_manifest.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)
