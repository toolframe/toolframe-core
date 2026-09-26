"""Small durable management journal, physically independent of business stores."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from toolframe_sdk import PluginError


def encode(value):
    return json.dumps(value, separators=(",", ":"), allow_nan=False)


class OperationQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=10, ge=1, le=100)
    through: int | None = Field(default=None, ge=0)
    target: str | None = Field(default=None, max_length=256)
    kind: str | None = Field(default=None, max_length=64)
    status: str | None = Field(default=None, max_length=64)


class SQLiteCoreState:
    def __init__(self, home: Path):
        path = home / "data" / "core.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        path.chmod(0o600)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            self.connection.close()
            raise PluginError(
                "core_schema_incompatible", f"Unsupported core schema {version}"
            )
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS candidates(
                id TEXT PRIMARY KEY, plugin_id TEXT NOT NULL, created REAL NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS plugins(
                id TEXT PRIMARY KEY, current_candidate TEXT, previous_candidate TEXT,
                last_candidate TEXT, runtime TEXT NOT NULL DEFAULT '{}', stopped INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS operations(
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, target TEXT, status TEXT NOT NULL,
                phase TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                request TEXT NOT NULL, result TEXT, error TEXT, idempotency TEXT UNIQUE
            );
        """)
        # Serialize the migration and recheck after acquiring the write lock.
        # UUIDs, payloads and idempotency keys survive; old records get stable
        # numbers in creation order, with insertion order breaking timestamp ties.
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            if self.connection.execute("PRAGMA user_version").fetchone()[0] < 2:
                self.connection.execute(
                    "ALTER TABLE operations RENAME TO operations_v1"
                )
                self.connection.execute("""
                    CREATE TABLE operations(
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, target TEXT,
                        status TEXT NOT NULL, phase TEXT NOT NULL,
                        created REAL NOT NULL, updated REAL NOT NULL,
                        request TEXT NOT NULL, result TEXT, error TEXT,
                        idempotency TEXT UNIQUE
                    )
                """)
                self.connection.execute("""
                    INSERT INTO operations(id,kind,target,status,phase,created,updated,request,result,error,idempotency)
                    SELECT id,kind,target,status,phase,created,updated,request,result,error,idempotency
                    FROM operations_v1 ORDER BY created, rowid
                """)
                self.connection.execute("DROP TABLE operations_v1")
                self.connection.execute("PRAGMA user_version=2")
            for field in ("target", "kind", "status"):
                self.connection.execute(
                    f"CREATE INDEX IF NOT EXISTS operations_{field}_sequence ON operations({field}, sequence)"
                )
        self.connection.commit()

    def close(self):
        self.connection.close()

    def meta(self, key, default=None):
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO metadata VALUES(?,?)", (key, encode(value))
            )

    def save_candidate(self, candidate):
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO candidates VALUES(?,?,?,?)",
                (
                    candidate["id"],
                    candidate["plugin_id"],
                    time.time(),
                    encode(candidate),
                ),
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO plugins(id) VALUES(?)", (candidate["plugin_id"],)
            )
            self.connection.execute(
                "UPDATE plugins SET last_candidate=? WHERE id=?",
                (candidate["id"], candidate["plugin_id"]),
            )

    def candidate(self, identifier):
        row = self.connection.execute(
            "SELECT data FROM candidates WHERE id=?", (identifier,)
        ).fetchone()
        if not row:
            raise PluginError("candidate_missing", f"Unknown candidate {identifier}")
        return json.loads(row[0])

    def plugin(self, identifier):
        row = self.connection.execute(
            "SELECT * FROM plugins WHERE id=?", (identifier,)
        ).fetchone()
        if not row:
            return {
                "id": identifier,
                "current_candidate": None,
                "previous_candidate": None,
                "last_candidate": None,
                "runtime": {},
                "stopped": False,
            }
        result = dict(row)
        result["runtime"] = json.loads(result["runtime"])
        result["stopped"] = bool(result["stopped"])
        return result

    def plugins(self):
        return [
            self.plugin(row[0])
            for row in self.connection.execute("SELECT id FROM plugins ORDER BY id")
        ]

    def runtime(self, identifier, value):
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO plugins(id) VALUES(?)", (identifier,)
            )
            self.connection.execute(
                "UPDATE plugins SET runtime=? WHERE id=?", (encode(value), identifier)
            )

    def select(self, identifier, candidate_id):
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO plugins(id) VALUES(?)", (identifier,)
            )
            self.connection.execute(
                "UPDATE plugins SET previous_candidate=current_candidate, current_candidate=? WHERE id=?",
                (candidate_id, identifier),
            )

    def stopped(self, identifier, stopped):
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO plugins(id) VALUES(?)", (identifier,)
            )
            self.connection.execute(
                "UPDATE plugins SET stopped=? WHERE id=?", (bool(stopped), identifier)
            )

    def operation(self, identifier):
        row = self.connection.execute(
            "SELECT * FROM operations WHERE id=?", (identifier,)
        ).fetchone()
        if not row:
            raise PluginError("operation_missing", f"Unknown operation {identifier}")
        result = dict(row)
        for field in ("request", "result", "error"):
            result[field] = (
                json.loads(result[field]) if result[field] is not None else None
            )
        return result

    def idempotent_operation(self, kind, target, request, idempotency=None):
        if idempotency:
            row = self.connection.execute(
                "SELECT id FROM operations WHERE idempotency=?", (idempotency,)
            ).fetchone()
            if row:
                existing = self.operation(row[0])
                if (
                    existing["kind"] != kind
                    or existing["target"] != target
                    or existing["request"] != request
                ):
                    raise PluginError(
                        "idempotency_conflict", "Key already belongs to another request"
                    )
                return existing
        return None

    def new_operation(self, kind, target, request, idempotency=None):
        existing = self.idempotent_operation(kind, target, request, idempotency)
        if existing is not None:
            return existing, False
        identifier = uuid.uuid4().hex
        now = time.time()
        with self.connection:
            self.connection.execute(
                "INSERT INTO operations(id,kind,target,status,phase,created,updated,request,result,error,idempotency) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    kind,
                    target,
                    "queued",
                    "queued",
                    now,
                    now,
                    encode(request),
                    None,
                    None,
                    idempotency,
                ),
            )
        return self.operation(identifier), True

    def update_operation(
        self, identifier, *, status=None, phase=None, result=None, error=None
    ):
        values = {"updated": time.time()}
        for key, value in (
            ("status", status),
            ("phase", phase),
            ("result", result),
            ("error", error),
        ):
            if value is not None:
                values[key] = encode(value) if key in ("result", "error") else value
        assignments = ",".join(f"{key}=?" for key in values)
        with self.connection:
            self.connection.execute(
                f"UPDATE operations SET {assignments} WHERE id=?",
                (*values.values(), identifier),
            )

    def operations(self, limit=50):
        return [
            self.operation(row[0])
            for row in self.connection.execute(
                "SELECT id FROM operations ORDER BY sequence DESC LIMIT ?", (limit,)
            )
        ]

    def operation_page(self, params=None):
        try:
            query = OperationQuery.model_validate(params or {})
        except ValidationError as error:
            raise PluginError(
                "invalid_request", "Invalid operation history query"
            ) from error
        latest = self.connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM operations"
        ).fetchone()[0]
        through = min(query.through, latest) if query.through is not None else latest
        clauses, values = ["sequence <= ?"], [through]
        for field in ("target", "kind", "status"):
            value = getattr(query, field)
            if value is not None:
                if field == "target" and value == "":
                    clauses.append("target IS NULL")
                else:
                    clauses.append(f"{field}=?")
                    values.append(value)
        where = " AND ".join(clauses)
        total = self.connection.execute(
            f"SELECT COUNT(*) FROM operations WHERE {where}", values
        ).fetchone()[0]
        pages = max(1, (total + query.page_size - 1) // query.page_size)
        page = min(query.page, pages)
        items = [
            self.operation(row[0])
            for row in self.connection.execute(
                f"SELECT id FROM operations WHERE {where} ORDER BY sequence DESC LIMIT ? OFFSET ?",
                (*values, query.page_size, (page - 1) * query.page_size),
            )
        ]
        filters = {
            field: [
                row[0]
                for row in self.connection.execute(
                    f"SELECT DISTINCT {field} FROM operations WHERE {field} IS NOT NULL ORDER BY {field}"
                )
            ]
            for field in ("target", "kind", "status")
        }
        return {
            "items": items,
            "page": page,
            "page_size": query.page_size,
            "pages": pages,
            "total": total,
            "through": through,
            "newer_count": self.connection.execute(
                "SELECT COUNT(*) FROM operations WHERE sequence > ?", (through,)
            ).fetchone()[0],
            "filters": filters,
        }

    def reconcile_startup(self, host_run):
        with self.connection:
            self.connection.execute(
                "UPDATE operations SET status='interrupted', updated=? WHERE status IN ('queued','running','cancelling')",
                (time.time(),),
            )
            for plugin in self.plugins():
                historical = plugin["runtime"]
                self.runtime(
                    plugin["id"],
                    {
                        "state": "unknown",
                        "host_run": host_run,
                        "previous_observation": historical,
                    },
                )
            self.connection.execute("UPDATE plugins SET stopped=0")
