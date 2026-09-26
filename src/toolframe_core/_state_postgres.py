"""PostgreSQL management journal; a dedicated database and fenced single owner."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid

from pydantic import ValidationError
from toolframe_sdk import PluginError

from ._state_sqlite import OperationQuery, encode

SCHEMA_VERSION = 1
SCHEMA_SQL = """
CREATE SCHEMA _toolframe_core;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
CREATE TABLE _toolframe_core.identity (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
    application_id TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    owner_run TEXT,
    owner_node TEXT
);
CREATE TABLE _toolframe_core.metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE _toolframe_core.candidates(
    id TEXT PRIMARY KEY, plugin_id TEXT NOT NULL, created DOUBLE PRECISION NOT NULL, data TEXT NOT NULL
);
CREATE TABLE _toolframe_core.plugins(
    id TEXT PRIMARY KEY, current_candidate TEXT, previous_candidate TEXT,
    last_candidate TEXT, runtime TEXT NOT NULL DEFAULT '{}', stopped BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE TABLE _toolframe_core.operations(
    sequence BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, target TEXT, status TEXT NOT NULL,
    phase TEXT NOT NULL, created DOUBLE PRECISION NOT NULL, updated DOUBLE PRECISION NOT NULL,
    request TEXT NOT NULL, result TEXT, error TEXT, idempotency TEXT UNIQUE
);
CREATE INDEX operations_target_sequence ON _toolframe_core.operations(target,sequence);
CREATE INDEX operations_kind_sequence ON _toolframe_core.operations(kind,sequence);
CREATE INDEX operations_status_sequence ON _toolframe_core.operations(status,sequence);
"""


def driver():
    try:
        import psycopg
    except ImportError:
        raise PluginError(
            "core_state_driver_missing",
            "Install toolframe-core[postgres] to use PostgreSQL",
        ) from None
    return psycopg


def lock_key(application_id):
    return int.from_bytes(
        hashlib.sha256(application_id.encode()).digest()[:8], signed=True
    )


def check_identity(row, application_id):
    if not row or row["application_id"] != application_id:
        raise PluginError(
            "core_state_identity_mismatch",
            "Core database belongs to a different application",
        )
    if row["schema_version"] != SCHEMA_VERSION:
        raise PluginError(
            "core_schema_incompatible", "Unsupported PostgreSQL Core schema"
        )


class PostgresCoreState:
    def __init__(self, settings, connection):
        self.settings, self.connection = settings, connection
        self.run = uuid.uuid4().hex
        self.failed = False
        self.claimed = False

    @classmethod
    async def open(cls, settings):
        pg = driver()
        from psycopg.rows import dict_row

        connection = None
        state = None
        try:
            async with asyncio.timeout(10):
                connection = await pg.AsyncConnection.connect(
                    settings.database_url,
                    autocommit=True,
                    row_factory=dict_row,
                    connect_timeout=5,
                )
                state = cls(settings, connection)
                await connection.execute(
                    "SET search_path TO _toolframe_core, pg_catalog"
                )
                await connection.execute("SET statement_timeout TO '5s'")
                await connection.execute("SET lock_timeout TO '2s'")
                role = await state._one(
                    "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname=current_user"
                )
                if any(role.values()):
                    raise PluginError(
                        "core_state_role_invalid",
                        "Core runtime requires a restricted database role",
                    )
                acquired = await state._one(
                    "SELECT pg_try_advisory_lock(%s) AS acquired",
                    (lock_key(settings.application_id),),
                )
                if not acquired["acquired"]:
                    raise PluginError(
                        "host_already_running",
                        "Another Core owns this application database",
                    )
                async with connection.transaction():
                    row = await state._one(
                        "SELECT * FROM identity WHERE singleton FOR UPDATE"
                    )
                    check_identity(row, settings.application_id)
                    if row["owner_run"]:
                        raise PluginError(
                            "core_state_recovery_required",
                            "Previous Core did not release ownership; verify its workers stopped before explicit recovery",
                            {
                                "owner_run": row["owner_run"],
                                "owner_node": row["owner_node"],
                            },
                        )
                    await connection.execute(
                        "UPDATE identity SET owner_run=%s, owner_node=%s WHERE singleton",
                        (state.run, settings.node_id),
                    )
                state.claimed = True
            return state
        except BaseException as error:
            if connection:
                await connection.close()
            if isinstance(error, (pg.Error, TimeoutError)):
                raise PluginError(
                    "core_state_unavailable",
                    "Cannot open the provisioned Core database",
                ) from None
            raise

    async def _one(self, query, params=()):
        return await (await self.connection.execute(query, params)).fetchone()

    async def _all(self, query, params=()):
        return await (await self.connection.execute(query, params)).fetchall()

    async def call(self, method, *args, **kwargs):
        pg = driver()
        try:
            async with asyncio.timeout(10):
                async with self.connection.transaction():
                    row = await self._one(
                        "SELECT owner_run FROM identity WHERE singleton"
                    )
                    if not row or row["owner_run"] != self.run:
                        raise PluginError(
                            "core_state_ownership_lost",
                            "Core journal ownership was lost",
                        )
                    return await getattr(self, method)(*args, **kwargs)
        except BaseException as error:
            if isinstance(error, (pg.Error, TimeoutError, asyncio.CancelledError)) or (
                isinstance(error, PluginError)
                and error.code == "core_state_ownership_lost"
            ):
                self.failed = True
                await self.connection.close()
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise PluginError(
                    "core_state_unavailable",
                    "Core journal connection failed; a write may have committed. Recover and inspect the operation before retrying",
                ) from None
            raise

    async def close(self, *, clean=True):
        try:
            if (
                clean
                and self.claimed
                and not self.failed
                and not self.connection.closed
            ):
                async with asyncio.timeout(10):
                    await self.connection.execute(
                        "UPDATE identity SET owner_run=NULL, owner_node=NULL WHERE singleton AND owner_run=%s",
                        (self.run,),
                    )
        finally:
            await self.connection.close()

    async def ping(self):
        return True

    async def meta(self, key, default=None):
        row = await self._one("SELECT value FROM metadata WHERE key=%s", (key,))
        return json.loads(row["value"]) if row else default

    async def set_meta(self, key, value):
        await self.connection.execute(
            "INSERT INTO metadata(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
            (key, encode(value)),
        )

    async def save_candidate(self, candidate):
        await self.connection.execute(
            "INSERT INTO candidates(id,plugin_id,created,data) VALUES(%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET plugin_id=EXCLUDED.plugin_id,created=EXCLUDED.created,data=EXCLUDED.data",
            (candidate["id"], candidate["plugin_id"], time.time(), encode(candidate)),
        )
        await self._ensure_plugin(candidate["plugin_id"])
        await self.connection.execute(
            "UPDATE plugins SET last_candidate=%s WHERE id=%s",
            (candidate["id"], candidate["plugin_id"]),
        )

    async def candidate(self, identifier):
        row = await self._one("SELECT data FROM candidates WHERE id=%s", (identifier,))
        if not row:
            raise PluginError("candidate_missing", f"Unknown candidate {identifier}")
        return json.loads(row["data"])

    async def _ensure_plugin(self, identifier):
        await self.connection.execute(
            "INSERT INTO plugins(id) VALUES(%s) ON CONFLICT DO NOTHING", (identifier,)
        )

    async def plugin(self, identifier):
        row = await self._one("SELECT * FROM plugins WHERE id=%s", (identifier,))
        if not row:
            return {
                "id": identifier,
                "current_candidate": None,
                "previous_candidate": None,
                "last_candidate": None,
                "runtime": {},
                "stopped": False,
            }
        row["runtime"] = json.loads(row["runtime"])
        return row

    async def plugins(self):
        return [
            await self.plugin(row["id"])
            for row in await self._all("SELECT id FROM plugins ORDER BY id")
        ]

    async def runtime(self, identifier, value):
        await self._ensure_plugin(identifier)
        await self.connection.execute(
            "UPDATE plugins SET runtime=%s WHERE id=%s", (encode(value), identifier)
        )

    async def select(self, identifier, candidate_id):
        await self._ensure_plugin(identifier)
        await self.connection.execute(
            "UPDATE plugins SET previous_candidate=current_candidate,current_candidate=%s WHERE id=%s",
            (candidate_id, identifier),
        )

    async def stopped(self, identifier, stopped):
        await self._ensure_plugin(identifier)
        await self.connection.execute(
            "UPDATE plugins SET stopped=%s WHERE id=%s", (bool(stopped), identifier)
        )

    @staticmethod
    def _operation(row):
        for field in ("request", "result", "error"):
            row[field] = json.loads(row[field]) if row[field] is not None else None
        return row

    async def operation(self, identifier):
        row = await self._one("SELECT * FROM operations WHERE id=%s", (identifier,))
        if not row:
            raise PluginError("operation_missing", f"Unknown operation {identifier}")
        return self._operation(row)

    async def idempotent_operation(self, kind, target, request, idempotency=None):
        if idempotency:
            row = await self._one(
                "SELECT * FROM operations WHERE idempotency=%s", (idempotency,)
            )
            if row:
                existing = self._operation(row)
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

    async def new_operation(self, kind, target, request, idempotency=None):
        existing = await self.idempotent_operation(kind, target, request, idempotency)
        if existing is not None:
            return existing, False
        now, identifier = time.time(), uuid.uuid4().hex
        row = await self._one(
            "INSERT INTO operations(id,kind,target,status,phase,created,updated,request,idempotency) VALUES(%s,%s,%s,'queued','queued',%s,%s,%s,%s) RETURNING *",
            (identifier, kind, target, now, now, encode(request), idempotency),
        )
        return self._operation(row), True

    async def update_operation(
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
        assignments = ",".join(f"{key}=%s" for key in values)
        await self.connection.execute(
            f"UPDATE operations SET {assignments} WHERE id=%s",
            (*values.values(), identifier),
        )

    async def operations(self, limit=50):
        return [
            self._operation(row)
            for row in await self._all(
                "SELECT * FROM operations ORDER BY sequence DESC LIMIT %s", (limit,)
            )
        ]

    async def operation_page(self, params=None):
        try:
            query = OperationQuery.model_validate(params or {})
        except ValidationError as error:
            raise PluginError(
                "invalid_request", "Invalid operation history query"
            ) from error
        latest = (
            await self._one(
                "SELECT COALESCE(MAX(sequence),0) AS latest FROM operations"
            )
        )["latest"]
        through = min(query.through, latest) if query.through is not None else latest
        clauses, values = ["sequence <= %s"], [through]
        for field in ("target", "kind", "status"):
            value = getattr(query, field)
            if value is not None:
                if field == "target" and value == "":
                    clauses.append("target IS NULL")
                else:
                    clauses.append(f"{field}=%s")
                    values.append(value)
        where = " AND ".join(clauses)
        total = (
            await self._one(
                f"SELECT COUNT(*) AS total FROM operations WHERE {where}", values
            )
        )["total"]
        pages = max(1, (total + query.page_size - 1) // query.page_size)
        page = min(query.page, pages)
        items = [
            self._operation(row)
            for row in await self._all(
                f"SELECT * FROM operations WHERE {where} ORDER BY sequence DESC LIMIT %s OFFSET %s",
                (*values, query.page_size, (page - 1) * query.page_size),
            )
        ]
        filters = {
            field: [
                row[field]
                for row in await self._all(
                    f"SELECT DISTINCT {field} FROM operations WHERE {field} IS NOT NULL ORDER BY {field}"
                )
            ]
            for field in ("target", "kind", "status")
        }
        newer = (
            await self._one(
                "SELECT COUNT(*) AS count FROM operations WHERE sequence > %s",
                (through,),
            )
        )["count"]
        return {
            "items": items,
            "page": page,
            "page_size": query.page_size,
            "pages": pages,
            "total": total,
            "through": through,
            "newer_count": newer,
            "filters": filters,
        }

    async def reconcile_startup(self, host_run):
        await self.connection.execute(
            "UPDATE operations SET status='interrupted',updated=%s WHERE status IN ('queued','running','cancelling')",
            (time.time(),),
        )
        for plugin in await self.plugins():
            await self.runtime(
                plugin["id"],
                {
                    "state": "unknown",
                    "host_run": host_run,
                    "previous_observation": plugin["runtime"],
                },
            )
        await self.connection.execute("UPDATE plugins SET stopped=FALSE")
