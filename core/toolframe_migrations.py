"""Plugin schema declarations and durable, provider-backed migration coordination."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.tokens import TokenType
from toolframe_database import DashboardDatabaseError
from toolframe_idempotency import _RECEIPT_SCHEMA

_OWNER = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_REVISION = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_FILENAME = re.compile(r"^(\d{4})_[a-z][a-z0-9_]*\.sql$")
_MAX_SOURCE_BYTES = 4 * 1024 * 1024
_LOCK = "toolframe:schema-coordinator:v1"
_META_TABLES = {
    "_toolframe_migration_artifacts",
    "_toolframe_schema_revisions",
    "_toolframe_schema_owners",
    "_toolframe_migration_runs",
}
_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS _toolframe_migrations (
    version INTEGER PRIMARY KEY CHECK (version BETWEEN 0 AND 9999),
    filename TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ'))
) STRICT;
"""


class MigrationError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        self.code = code
        self.retryable = retryable
        super().__init__(message)


def _error(code, message):
    raise MigrationError(code, message)


def _canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _now():
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _reference(value):
    if not isinstance(value, str) or len(value.split("@")) != 2:
        _error(
            "invalid_reference",
            "Migration dependencies must use owner@revision references.",
        )
    owner, revision = value.split("@")
    if not _OWNER.fullmatch(owner) or not _REVISION.fullmatch(revision):
        _error("invalid_reference", "A migration reference is invalid.")
    return value


@dataclass(frozen=True)
class Revision:
    """Immutable migration inputs; source files are archived before execution."""

    revision: str
    sql: Mapping[str, Path]
    tables: tuple[str, ...]
    depends: tuple[str, ...] = ()
    reads: tuple[str, ...] = ()


@dataclass(frozen=True)
class Schema:
    """An installed schema owner and the revision required by its code."""

    owner: str
    revisions: tuple[Revision, ...]
    target: str
    compatible_heads: tuple[str, ...] = ()


@dataclass(frozen=True)
class Artifact:
    metadata: Mapping[str, Any]
    sources: Mapping[str, Path] = field(default_factory=dict, compare=False, repr=False)

    @property
    def ref(self):
        return self.metadata["ref"]

    @property
    def owner(self):
        return self.metadata["owner"]

    @property
    def sha256(self):
        return _hash(self.payload.encode())

    @property
    def payload(self):
        return _canonical(self.metadata)

    def sql_for(self, dialect):
        source = self.metadata["sql"].get(dialect)
        if source is None:
            _error(
                "missing_provider_sql",
                f"Migration {self.ref} has no SQL for {dialect}.",
            )
        try:
            data = base64.b64decode(source["base64"], validate=True)
            if _hash(data) != source["sha256"]:
                _error("artifact_corrupt", f"SQL checksum mismatch in {self.ref}.")
            return data.decode("utf-8")
        except (UnicodeError, ValueError, KeyError):
            _error("artifact_corrupt", f"SQL artifact is invalid for {self.ref}.")

    def verify_sources(self):
        for dialect, path in self.sources.items():
            if _hash(_read_source(path)) != self.metadata["sql"][dialect]["sha256"]:
                _error(
                    "source_changed", f"Migration {self.ref} changed during startup."
                )


def _read_source(path: Path) -> bytes:
    path = Path(path)
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size > _MAX_SOURCE_BYTES
    ):
        _error(
            "invalid_source",
            f"Migration source must be a regular file no larger than {_MAX_SOURCE_BYTES} bytes: {path.name}",
        )
    data = path.read_bytes()
    if len(data) > _MAX_SOURCE_BYTES:
        _error("invalid_source", "Migration source grew beyond the size limit.")
    try:
        data.decode("utf-8")
    except UnicodeError:
        _error("invalid_source", f"Migration source must be UTF-8: {path.name}")
    return data


def _artifact(
    owner: str, revision: Revision, *, legacy_version=None, legacy_filename=None
):
    if (
        not isinstance(owner, str)
        or not _OWNER.fullmatch(owner)
        or owner in {"legacy", "toolframe"}
    ):
        _error(
            "invalid_owner",
            "Schema owners must be stable lowercase business identifiers.",
        )
    ref = _reference(
        f"{'legacy' if legacy_version is not None else owner}@{revision.revision}"
    )
    if not revision.tables or len(set(revision.tables)) != len(revision.tables):
        _error(
            "invalid_ownership", f"Migration {ref} must declare distinct owned tables."
        )
    for table in revision.tables:
        if (not _valid_table(table) or not table.startswith(owner + "_")) and not (
            legacy_version is not None
            and owner == "entry_register"
            and table == "dashboard_example_entries"
        ):
            _error(
                "invalid_ownership",
                f"Table {table!r} must use the {owner}_ prefix.",
            )
    if any(
        not _valid_table(table) or table.startswith(("_toolframe_", "sqlite_", "pg_"))
        for table in revision.reads
    ):
        _error(
            "invalid_read_contract", f"Migration {ref} has an invalid read contract."
        )
    if set(revision.sql) - {"sqlite", "postgresql"} or not revision.sql:
        _error(
            "invalid_provider_sql",
            f"Migration {ref} must declare supported SQL source variants.",
        )
    sources = {dialect: Path(path) for dialect, path in revision.sql.items()}
    encoded = {}
    for dialect, path in sources.items():
        data = _read_source(path)
        encoded[dialect] = {
            "filename": path.name,
            "sha256": _hash(data),
            "base64": base64.b64encode(data).decode("ascii"),
        }
    metadata = {
        "format": 1,
        "ref": ref,
        "owner": owner,
        "tables": sorted(revision.tables),
        "reads": sorted(revision.reads),
        "depends": sorted({_reference(item) for item in revision.depends}),
        "sql": encoded,
        "legacy_version": legacy_version,
        "legacy_filename": legacy_filename,
    }
    return Artifact(metadata, sources)


def _valid_table(value):
    return (
        isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]*", value) is not None
    )


def read_schema(path: Path) -> Schema:
    """Read a domain/package schema.json whose SQL paths are relative to it."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        _error("invalid_manifest", "Schema manifests must be regular files.")
    try:
        content = json.loads(path.read_text())
        if set(content) - {"owner", "target", "compatible_heads", "revisions"}:
            raise ValueError("unknown schema fields")
        revisions = []
        for item in content["revisions"]:
            if set(item) - {"revision", "sql", "tables", "depends", "reads"}:
                raise ValueError("unknown revision fields")
            sources = {}
            for dialect, source in item["sql"].items():
                candidate = path.parent / source
                if Path(source).is_absolute() or ".." in Path(source).parts:
                    raise ValueError("source must stay inside its package")
                sources[dialect] = candidate
            revisions.append(
                Revision(
                    item["revision"],
                    sources,
                    tuple(item["tables"]),
                    tuple(item.get("depends", ())),
                    tuple(item.get("reads", ())),
                )
            )
        return Schema(
            content["owner"],
            tuple(revisions),
            content["target"],
            tuple(content.get("compatible_heads", ())),
        )
    except (KeyError, TypeError, ValueError):
        _error("invalid_manifest", f"Invalid schema manifest: {path.name}")


def discover_domains(domains_dir: Path):
    """Load namespaced manifests and explicitly preserve unclaimed legacy SQL."""
    schemas, legacy = [], []
    directories = sorted(
        path
        for path in domains_dir.iterdir()
        if path.is_dir() and not path.name.startswith((".", "_"))
    )
    for domain in directories:
        manifest = domain / "migrations" / "schema.json"
        if manifest.exists():
            schema = read_schema(manifest)
            if schema.owner != domain.name:
                _error(
                    "invalid_owner",
                    "Domain schema ownership must match the business directory name.",
                )
            schemas.append(schema)
    claimed = {
        Path(path).resolve()
        for schema in schemas
        for revision in schema.revisions
        for path in revision.sql.values()
    }
    for domain in directories:
        directory = domain / "migrations"
        if directory.is_symlink():
            _error("invalid_source", "Migration directories must not be symlinks.")
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.sql")):
            if path.resolve() in claimed:
                continue
            match = _FILENAME.fullmatch(path.name)
            if not match:
                _error(
                    "invalid_filename",
                    f"Invalid legacy migration filename: {path.name}",
                )
            version = int(match[1])
            sql = _read_source(path).decode()
            # Legacy ownership is inferred only at the explicit adoption boundary.
            # New revisions always declare it. The preserved example bootstrap
            # table is the sole historical business-prefix exception.
            tables = _script_tables(sql, "sqlite")
            sources = {"sqlite": path}
            companion = directory / "postgresql" / path.name
            if companion.exists():
                sources["postgresql"] = companion
            revision = Revision(
                f"{version:04}",
                sources,
                tuple(sorted(tables)),
                (f"legacy@{version - 1:04}",) if version else (),
            )
            legacy.append(
                _artifact(
                    domain.name,
                    revision,
                    legacy_version=version,
                    legacy_filename=path.name,
                )
            )
    versions = [
        item.metadata["legacy_version"]
        for item in sorted(legacy, key=lambda item: item.ref)
    ]
    if len(versions) != len(set(versions)):
        _error("duplicate_revision", "Legacy global migration numbers must be unique.")
    if any(
        item.ref == "legacy@0000"
        and item.metadata["legacy_filename"] != "0000_bootstrap.sql"
        for item in legacy
    ):
        _error(
            "invalid_filename", "Legacy revision 0000 must keep its bootstrap filename."
        )
    return schemas, legacy


def _script_tables(sql, dialect):
    try:
        return {
            table.name
            for tree in _parse(sql, dialect)
            if tree is not None
            for table in tree.find_all(exp.Table)
        }
    except sqlglot.errors.SqlglotError:
        _error("invalid_sql", "A migration has invalid SQL syntax.")


def _parse(sql, dialect):
    parser_dialect = "postgres" if dialect == "postgresql" else dialect
    tokens = sqlglot.Dialect.get_or_raise(parser_dialect).tokenize(sql)
    # SQLGlot's SQLite dialect omits table-rename parsing. Its PostgreSQL parser
    # understands this shared ALTER syntax; execution still uses original SQL.
    if parser_dialect == "sqlite" and tokens and tokens[0].text.upper() == "ALTER":
        parser_dialect = "postgres"
    return sqlglot.parse(sql, read=parser_dialect)


def _rows(connection, sql, params=()):
    cursor = connection.execute(sql, params)
    try:
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        cursor.close()


def _table_names(connection, dialect):
    sql = (
        "SELECT name FROM sqlite_master WHERE type='table'"
        if dialect == "sqlite"
        else "SELECT tablename AS name FROM pg_tables WHERE schemaname='public'"
    )
    return {row["name"] for row in _rows(connection, sql)}


def _execute(connection, sql, params=()):
    connection.execute(sql, params).close()


@dataclass
class Plan:
    pending: list[Artifact]
    applied: dict[str, Artifact]
    installed: dict[str, Artifact]
    owners: dict[str, str]
    legacy: list[dict[str, Any]]

    def public_info(self):
        return {
            "pending": [artifact.ref for artifact in self.pending],
            "applied": sorted(self.applied),
            "total": len(set(self.installed) | set(self.applied)),
        }


def _order(roots, available):
    """Order revision graphs without a Python recursion-depth limit."""
    ordered, visiting, visited = [], set(), set()
    for root in sorted(roots):
        stack = [(root, False)]
        while stack:
            ref, complete = stack.pop()
            if ref in visited:
                continue
            artifact = available.get(ref)
            if artifact is None:
                _error("missing_dependency", f"Migration dependency is missing: {ref}")
            if complete:
                visiting.remove(ref)
                visited.add(ref)
                ordered.append(artifact)
                continue
            if ref in visiting:
                _error("dependency_cycle", f"Migration dependency cycle at {ref}.")
            visiting.add(ref)
            stack.append((ref, True))
            stack.extend(
                (dependency, False)
                for dependency in reversed(artifact.metadata["depends"])
            )
    return ordered


class MigrationCoordinator:
    def __init__(
        self, provider, schemas: Sequence[Schema], *, legacy: Sequence[Artifact] = ()
    ):
        self.provider = provider
        self.schemas = tuple(schemas)
        if len({schema.owner for schema in schemas}) != len(schemas):
            _error(
                "duplicate_owner",
                "Only one installed schema declaration may claim an owner.",
            )
        artifacts = [
            _artifact(schema.owner, revision)
            for schema in schemas
            for revision in schema.revisions
        ]
        artifacts.extend(legacy)
        self.installed = {artifact.ref: artifact for artifact in artifacts}
        if len(self.installed) != len(artifacts):
            _error(
                "duplicate_revision", "Installed migration references must be unique."
            )
        self.targets = {
            _reference(f"{schema.owner}@{schema.target}") for schema in schemas
        }
        if legacy:
            self.targets.add(max(legacy, key=lambda artifact: artifact.ref).ref)
        for target in self.targets:
            if target not in self.installed:
                _error(
                    "missing_target", f"Installed schema target is missing: {target}"
                )

    def _load(self, connection):
        names = _table_names(connection, self.provider.dialect)
        present = names & _META_TABLES
        if present and present != _META_TABLES:
            _error(
                "history_schema_corrupt",
                "The durable migration metadata schema is incomplete.",
            )
        archives, applied, owners = {}, {}, {}
        if present:
            for row in _rows(
                connection, "SELECT sha256, payload FROM _toolframe_migration_artifacts"
            ):
                if _hash(row["payload"].encode()) != row["sha256"]:
                    _error(
                        "artifact_corrupt",
                        "An archived migration artifact failed its checksum.",
                    )
                try:
                    artifact = Artifact(json.loads(row["payload"]))
                    for dialect in artifact.metadata["sql"]:
                        artifact.sql_for(dialect)
                    if artifact.sha256 != row["sha256"]:
                        raise ValueError("noncanonical artifact")
                    archives[row["sha256"]] = artifact
                except (KeyError, ValueError, TypeError):
                    _error(
                        "artifact_corrupt",
                        "An archived migration artifact is malformed.",
                    )
            for row in _rows(
                connection,
                "SELECT ref, owner, artifact_sha FROM _toolframe_schema_revisions",
            ):
                artifact = archives.get(row["artifact_sha"])
                if (
                    artifact is None
                    or artifact.ref != row["ref"]
                    or artifact.owner != row["owner"]
                ):
                    _error(
                        "missing_artifact",
                        "An applied migration has no matching durable artifact.",
                    )
                applied[artifact.ref] = artifact
            owners = {
                row["table_name"]: row["owner"]
                for row in _rows(
                    connection, "SELECT table_name, owner FROM _toolframe_schema_owners"
                )
            }
        legacy = (
            _rows(
                connection,
                "SELECT version, filename, sha256, applied_at FROM _toolframe_migrations ORDER BY version",
            )
            if "_toolframe_migrations" in names
            else []
        )
        for expected, row in enumerate(legacy):
            if row["version"] != expected:
                _error(
                    "legacy_history_gap",
                    "The legacy applied ledger has a numbering gap.",
                )
            ref = f"legacy@{expected:04}"
            artifact = self.installed.get(ref) or applied.get(ref)
            if artifact is None:
                _error(
                    "missing_legacy_source",
                    f"Original source is required to adopt {ref}.",
                )
            if (
                artifact.metadata["legacy_filename"] != row["filename"]
                or artifact.metadata["sql"]["sqlite"]["sha256"] != row["sha256"]
            ):
                _error(
                    "legacy_checksum_mismatch",
                    f"Legacy migration {ref} was renamed or changed.",
                )
            applied.setdefault(ref, artifact)
        for ref, artifact in applied.items():
            if artifact.metadata["legacy_version"] is not None and ref not in {
                f"legacy@{row['version']:04}" for row in legacy
            }:
                _error(
                    "legacy_history_missing",
                    "The preserved legacy ledger is missing an applied row.",
                )
        return applied, owners, legacy, names

    def plan(self, connection) -> Plan:
        applied, owners, legacy, names = self._load(connection)
        available = {**applied, **self.installed}
        # Removed domains can leave holes in the installed file sequence. Only
        # integrity-checked, applied history may fill them; pending archives do not.
        versions = sorted(
            artifact.metadata["legacy_version"]
            for artifact in available.values()
            if artifact.metadata["legacy_version"] is not None
        )
        if versions and versions != list(range(versions[-1] + 1)):
            _error(
                "legacy_history_gap",
                "Installed sources and applied legacy history must be contiguous from 0000.",
            )
        for ref, previous in applied.items():
            current = self.installed.get(ref)
            if current is not None and current.sha256 != previous.sha256:
                _error(
                    "checksum_mismatch",
                    f"Applied migration {ref} changed; restore it and add a new revision.",
                )
            if any(
                dependency not in applied for dependency in previous.metadata["depends"]
            ):
                _error(
                    "applied_dependency_missing",
                    f"Applied history has a missing dependency for {ref}.",
                )
        expected_owners = dict(owners)
        for artifact in available.values():
            for table in artifact.metadata["tables"]:
                if (
                    table in expected_owners
                    and expected_owners[table] != artifact.owner
                ):
                    _error(
                        "ownership_conflict",
                        f"Table {table} is already reserved by {expected_owners[table]}.",
                    )
                expected_owners[table] = artifact.owner
        # Applied ownership must remain durable, including after plugin removal.
        if names & _META_TABLES:
            for artifact in applied.values():
                for table in artifact.metadata["tables"]:
                    if owners.get(table) != artifact.owner:
                        _error(
                            "ownership_history_missing",
                            f"Durable ownership is missing for {table}.",
                        )
        order = _order(self.targets, available)
        # Applied archives remain verifiable even when their plugins are absent.
        _order(applied, applied)
        for schema in self.schemas:
            covered = {
                item.ref
                for item in _order([f"{schema.owner}@{schema.target}"], available)
            }
            extra = {
                ref
                for ref, artifact in {
                    **applied,
                    **{item.ref: item for item in order},
                }.items()
                if artifact.owner == schema.owner
                and ref not in covered
                and not ref.startswith("legacy@")
            }
            compatible = {
                _reference(f"{schema.owner}@{head}") for head in schema.compatible_heads
            }

            allowed = {item.ref for item in _order(compatible, available)}
            if extra - allowed:
                _error(
                    "incompatible_schema",
                    f"Installed code for {schema.owner} targets an older or divergent schema.",
                )
        pending = [artifact for artifact in order if artifact.ref not in applied]
        for artifact in pending:
            validate_script(artifact, self.provider.dialect, expected_owners)
        return Plan(pending, applied, self.installed, expected_owners, legacy)

    def check(self):
        if (
            self.provider.dialect == "sqlite"
            and not self.provider.settings.sqlite_path.exists()
        ):
            # Planning a new SQLite target does not create it.
            with closing(sqlite3.connect(":memory:")) as connection:
                return self.plan(connection).public_info()
        with self.provider.connection(read_only=True) as connection:
            return self.plan(connection).public_info()

    def apply(self):
        if self.provider.dialect == "sqlite":
            self.provider.settings.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        run_id = uuid.uuid4().hex
        try:
            with self.provider.connection() as connection:
                self.provider.begin_locked(connection, _LOCK)
                try:
                    plan = self.plan(connection)
                    _initialize_history(connection, self.provider.dialect)
                    for artifact in self.installed.values():
                        _execute(
                            connection,
                            "INSERT INTO _toolframe_migration_artifacts (sha256,payload,stored_at) VALUES (:hash,:payload,:now) ON CONFLICT (sha256) DO NOTHING",
                            {
                                "hash": artifact.sha256,
                                "payload": artifact.payload,
                                "now": _now(),
                            },
                        )
                    for table, owner in plan.owners.items():
                        _execute(
                            connection,
                            "INSERT INTO _toolframe_schema_owners (table_name,owner) VALUES (:table,:owner) ON CONFLICT (table_name) DO NOTHING",
                            {"table": table, "owner": owner},
                        )
                    for row in plan.legacy:
                        artifact = plan.applied[f"legacy@{row['version']:04}"]
                        _record_revision(connection, artifact, row["applied_at"])
                    _execute(
                        connection,
                        "INSERT INTO _toolframe_migration_runs (id,status,targets_json,started_at) VALUES (:id,'prepared',:targets,:now)",
                        {
                            "id": run_id,
                            "targets": _canonical(sorted(self.targets)),
                            "now": _now(),
                        },
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            # Artifact payloads, ownership, and the prepared run are committed
            # before any plugin SQL executes. Reacquire the lock and re-plan:
            # another process may have applied these revisions meanwhile.
            with self.provider.connection() as connection:
                self.provider.begin_locked(connection, _LOCK)
                try:
                    plan = self.plan(connection)
                    for artifact in self.installed.values():
                        artifact.verify_sources()
                    for artifact in plan.pending:
                        execute_script(
                            connection, artifact, self.provider.dialect, plan.owners
                        )
                        _record_revision(connection, artifact, _now())
                        if artifact.metadata["legacy_version"] is not None:
                            _execute(
                                connection,
                                "INSERT INTO _toolframe_migrations (version,filename,sha256,applied_at) VALUES (:version,:filename,:hash,:now)",
                                {
                                    "version": artifact.metadata["legacy_version"],
                                    "filename": artifact.metadata["legacy_filename"],
                                    "hash": artifact.metadata["sql"]["sqlite"][
                                        "sha256"
                                    ],
                                    "now": _now(),
                                },
                            )
                    _execute(
                        connection,
                        "UPDATE _toolframe_migration_runs SET status='applied',finished_at=:now WHERE id=:id",
                        {"id": run_id, "now": _now()},
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            return {
                **plan.public_info(),
                "pending": [],
                "applied": sorted(
                    set(plan.applied) | {artifact.ref for artifact in plan.pending}
                ),
                "appliedNow": [artifact.ref for artifact in plan.pending],
                "runId": run_id,
            }
        except MigrationError as error:
            self._record_failure(run_id, error.code)
            raise
        except Exception as error:  # noqa: BLE001 - normalize raw provider migration errors.
            self._record_failure(run_id, "migration_sql_failed")
            try:
                public = self.provider.normalize_error(
                    error, operation="migration execution"
                )
                if not isinstance(public, DashboardDatabaseError):
                    raise TypeError("invalid provider error contract")
            except Exception as normalization_error:  # noqa: BLE001 - third-party hooks must not mask failures or reveal secrets.
                logging.getLogger(__name__).error(
                    "migration error normalization failed exception_type=%s",
                    type(normalization_error).__name__,
                )
                public = DashboardDatabaseError(
                    "database_error", "The database could not execute the migration."
                )
            raise MigrationError(
                "migration_sql_failed", public.message, retryable=public.retryable
            ) from None

    def _record_failure(self, run_id, code):
        try:
            with self.provider.connection() as connection:
                if "_toolframe_migration_runs" in _table_names(
                    connection, self.provider.dialect
                ):
                    _execute(
                        connection,
                        "UPDATE _toolframe_migration_runs SET status='failed',error_code=:code,finished_at=:now WHERE id=:id",
                        {"id": run_id, "code": code, "now": _now()},
                    )
                    connection.commit()
        except Exception as error:  # noqa: BLE001 - preserve the original failure without secrets.
            logging.getLogger(__name__).error(
                "migration failure record unavailable exception_type=%s",
                type(error).__name__,
            )


def _record_revision(connection, artifact, applied_at):
    _execute(
        connection,
        "INSERT INTO _toolframe_schema_revisions (ref,owner,artifact_sha,applied_at) VALUES (:ref,:owner,:hash,:now) ON CONFLICT (ref) DO NOTHING",
        {
            "ref": artifact.ref,
            "owner": artifact.owner,
            "hash": artifact.sha256,
            "now": applied_at,
        },
    )


def _initialize_history(connection, dialect):
    statements = [
        "CREATE TABLE IF NOT EXISTS _toolframe_migration_artifacts (sha256 TEXT PRIMARY KEY, payload TEXT NOT NULL, stored_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS _toolframe_schema_revisions (ref TEXT PRIMARY KEY, owner TEXT NOT NULL, artifact_sha TEXT NOT NULL REFERENCES _toolframe_migration_artifacts(sha256), applied_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS _toolframe_schema_owners (table_name TEXT PRIMARY KEY, owner TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS _toolframe_migration_runs (id TEXT PRIMARY KEY, status TEXT NOT NULL CHECK(status IN ('prepared','applied','failed')), targets_json TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, error_code TEXT)",
    ]
    if dialect == "sqlite":
        # Preserve the original ledger and receipt schema, not just its rows.
        for name, definition in (
            ("_toolframe_migrations", _LEDGER_SCHEMA),
            ("_toolframe_idempotency", _RECEIPT_SCHEMA),
        ):
            existing = _rows(
                connection,
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=:name",
                {"name": name},
            )
            canonical = lambda value: (
                re.sub(r"\s+", "", value.casefold())
                .replace("ifnotexists", "")
                .rstrip(";")
            )
            if existing and canonical(existing[0]["sql"]) != canonical(definition):
                _error(
                    "legacy_schema_changed",
                    f"The preserved internal schema for {name} changed.",
                )
        statements.extend(
            (
                _LEDGER_SCHEMA,
                _RECEIPT_SCHEMA.replace(
                    "CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1
                ),
            )
        )
    else:
        statements.extend(
            (
                "CREATE TABLE IF NOT EXISTS _toolframe_migrations (version INTEGER PRIMARY KEY CHECK(version BETWEEN 0 AND 9999), filename TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL CHECK(length(sha256)=64), applied_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS _toolframe_idempotency (namespace TEXT NOT NULL, business_key TEXT NOT NULL, input_sha256 TEXT NOT NULL CHECK(length(input_sha256)=64), result_json BYTEA NOT NULL, result_sha256 TEXT NOT NULL CHECK(length(result_sha256)=64), result_bytes INTEGER NOT NULL CHECK(result_bytes BETWEEN 1 AND 1048576), original_operation_id TEXT NOT NULL, completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(namespace,business_key))",
            )
        )
    for statement in statements:
        _execute(connection, statement)


def _statements(sql, dialect):
    parser_dialect = "postgres" if dialect == "postgresql" else dialect
    try:
        tokens = sqlglot.Dialect.get_or_raise(parser_dialect).tokenize(sql)
        boundaries = [
            token.start for token in tokens if token.token_type == TokenType.SEMICOLON
        ]
        start, chunks = 0, []
        for end in [*boundaries, len(sql)]:
            chunk = sql[start:end].strip()
            start = end + 1
            trees = [tree for tree in _parse(chunk, parser_dialect) if tree is not None]
            if trees:
                if len(trees) != 1:
                    _error(
                        "invalid_sql",
                        "A migration statement could not be parsed unambiguously.",
                    )
                chunks.append((chunk, trees[0]))
        if not chunks:
            _error(
                "empty_migration", "A migration must contain an executable statement."
            )
        return chunks
    except sqlglot.errors.SqlglotError:
        _error("invalid_sql", "A migration contains unsupported or invalid SQL.")


def validate_script(artifact, dialect, owners):
    """Accept a deliberately bounded set of transactional schema/data SQL."""
    statements = _statements(artifact.sql_for(dialect), dialect)
    writes = set(artifact.metadata["tables"])
    reads = set(artifact.metadata["reads"])
    dependency_owners = {ref.split("@", 1)[0] for ref in artifact.metadata["depends"]}
    for table in reads - writes:
        owner = owners.get(table)
        if owner is None or owner not in dependency_owners:
            _error(
                "missing_read_dependency",
                f"Read contract for {table} needs an explicit revision dependency on its owner.",
            )
    safe_functions = {
        "abs",
        "avg",
        "cast",
        "ceil",
        "ceiling",
        "coalesce",
        "concat",
        "count",
        "current_date",
        "current_timestamp",
        "date",
        "datetime",
        "floor",
        "length",
        "lower",
        "max",
        "min",
        "nullif",
        "round",
        "strftime",
        "substr",
        "substring",
        "sum",
        "trim",
        "upper",
    }
    for sql, tree in statements:
        # A SELECT wrapper does not make a data-modifying CTE read-only.
        # Keep this transactional subset explicit: every mutation is its own
        # top-level statement, with one owned target checked below.
        if any(
            isinstance(node, (exp.Into, exp.Lock))
            or (
                node is not tree
                and isinstance(node, (exp.DML, exp.DDL, exp.Alter, exp.Drop))
            )
            for node in tree.walk()
        ):
            _error(
                "unsupported_migration_sql",
                "Nested mutations, SELECT INTO and explicit row locks are not supported in migrations; use separate owned statements.",
            )
        drop_index = (
            isinstance(tree, exp.Drop)
            and str(tree.args.get("kind", "")).upper() == "INDEX"
        )
        if not isinstance(
            tree,
            (
                exp.Create,
                exp.Alter,
                exp.Drop,
                exp.Insert,
                exp.Update,
                exp.Delete,
                exp.Select,
            ),
        ):
            _error(
                "unsupported_migration_sql",
                f"Migration {artifact.ref} uses a nontransactional or unsupported command.",
            )
        if isinstance(tree, (exp.Create, exp.Drop)) and str(
            tree.args.get("kind", "")
        ).upper() not in {"TABLE", "INDEX", "VIEW"}:
            _error(
                "unsupported_migration_sql",
                "Migrations support persistent tables, indexes and views only.",
            )
        if (
            isinstance(tree, exp.Alter)
            and str(tree.args.get("kind", "TABLE")).upper() != "TABLE"
        ):
            _error("unsupported_migration_sql", "Only table alterations are supported.")
        tokens = sqlglot.Dialect.get_or_raise(
            "postgres" if dialect == "postgresql" else dialect
        ).tokenize(sql)
        if any(
            token.text.upper()
            in {"CONCURRENTLY", "TEMPORARY", "TEMP", "UNLOGGED", "CASCADE"}
            and token.token_type not in {TokenType.STRING, TokenType.IDENTIFIER}
            for token in tokens
        ):
            _error(
                "unsupported_migration_sql",
                "Concurrent, temporary, unlogged and cascading schema operations are not supported.",
            )
        for table in tree.find_all(exp.Table):
            if drop_index and table is tree.this and not table.db and not table.catalog:
                if not table.name.startswith(artifact.owner + "_"):
                    _error(
                        "invalid_ownership",
                        "Index names must use their schema owner's prefix.",
                    )
                continue
            if table.db or table.catalog or table.name not in writes | reads:
                _error(
                    "table_access_denied",
                    f"Migration {artifact.ref} does not declare access to table {table.name!r}.",
                )
        # The mutation target must belong to this migration owner, even when
        # another table appears in its explicit read contract.
        target = tree.this
        if isinstance(target, exp.Schema):
            target = target.this
        if (
            isinstance(
                tree,
                (exp.Create, exp.Alter, exp.Drop, exp.Insert, exp.Update, exp.Delete),
            )
            and isinstance(target, exp.Table)
            and target.name not in writes
            and not drop_index
        ):
            _error(
                "table_write_denied",
                f"Migration {artifact.ref} cannot mutate another owner's table.",
            )
        for function in tree.find_all(exp.Func):
            name = (
                function.name.casefold()
                if isinstance(function, exp.Anonymous)
                else function.sql_name().casefold()
            )
            if name not in safe_functions:
                _error(
                    "unsupported_migration_function",
                    f"Migration {artifact.ref} uses an unsupported SQL function: {name}.",
                )
        for index in tree.find_all(exp.Index):
            if index.name and not index.name.startswith(artifact.owner + "_"):
                _error(
                    "invalid_ownership",
                    f"Index {index.name} must use the {artifact.owner}_ prefix.",
                )
    return statements


def execute_script(connection, artifact, dialect, owners):
    statements = validate_script(artifact, dialect, owners)
    for _, tree in statements:
        if (
            isinstance(tree, exp.Drop)
            and str(tree.args.get("kind", "")).upper() == "INDEX"
        ):
            lookup = (
                "SELECT tbl_name AS table_name FROM sqlite_master WHERE type='index' AND name=:name"
                if dialect == "sqlite"
                else "SELECT tablename AS table_name FROM pg_indexes WHERE schemaname='public' AND indexname=:name"
            )
            for row in _rows(connection, lookup, {"name": tree.this.name}):
                if row["table_name"] not in artifact.metadata["tables"]:
                    _error(
                        "table_write_denied",
                        "An index belongs to a table outside this migration's ownership.",
                    )
    if dialect == "sqlite":
        owned = set(artifact.metadata["tables"])
        readable = owned | set(artifact.metadata["reads"])
        internal = {
            "sqlite_master",
            "sqlite_schema",
            "sqlite_temp_master",
            "sqlite_sequence",
        }

        def authorize(action, first, second, database, trigger):
            if action in {
                sqlite3.SQLITE_ATTACH,
                sqlite3.SQLITE_DETACH,
                sqlite3.SQLITE_PRAGMA,
                sqlite3.SQLITE_TRANSACTION,
                sqlite3.SQLITE_SAVEPOINT,
                sqlite3.SQLITE_CREATE_VTABLE,
                sqlite3.SQLITE_DROP_VTABLE,
                sqlite3.SQLITE_CREATE_TRIGGER,
                sqlite3.SQLITE_DROP_TRIGGER,
            }:
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ:
                return (
                    sqlite3.SQLITE_OK
                    if first in readable | internal
                    else sqlite3.SQLITE_DENY
                )
            if action in {
                sqlite3.SQLITE_INSERT,
                sqlite3.SQLITE_UPDATE,
                sqlite3.SQLITE_DELETE,
            }:
                return (
                    sqlite3.SQLITE_OK
                    if first in owned | internal
                    else sqlite3.SQLITE_DENY
                )
            if action in {
                sqlite3.SQLITE_CREATE_TABLE,
                sqlite3.SQLITE_DROP_TABLE,
                sqlite3.SQLITE_CREATE_VIEW,
                sqlite3.SQLITE_DROP_VIEW,
            }:
                return (
                    sqlite3.SQLITE_OK
                    if first in owned
                    or (
                        action == sqlite3.SQLITE_CREATE_TABLE
                        and first == "sqlite_sequence"
                    )
                    else sqlite3.SQLITE_DENY
                )
            if action in {
                sqlite3.SQLITE_ALTER_TABLE,
                sqlite3.SQLITE_CREATE_INDEX,
                sqlite3.SQLITE_DROP_INDEX,
            }:
                return sqlite3.SQLITE_OK if second in owned else sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
    try:
        for sql, _ in statements:
            _execute(connection, sql)
    finally:
        if dialect == "sqlite":
            connection.set_authorizer(None)
