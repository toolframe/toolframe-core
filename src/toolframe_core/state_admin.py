"""Explicit provisioning and recovery for a dedicated Core PostgreSQL database."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from pathlib import Path

from toolframe_sdk import PluginError

from ._state_postgres import (
    SCHEMA_SQL,
    SCHEMA_VERSION,
    check_identity,
    driver,
    lock_key,
)
from .state import StateSettings

RUNTIME_KEYS = {
    "TOOLFRAME_CORE_STATE_BACKEND",
    "TOOLFRAME_CORE_DATABASE_URL",
    "TOOLFRAME_CORE_APPLICATION_ID",
    "TOOLFRAME_CORE_NODE_ID",
}


def setup(admin_dsn, *, database, application_id, node_id, write_environment=None):
    pg = driver()
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", database) or not all(
        isinstance(value, str) and 0 < len(value) <= 256
        for value in (application_id, node_id)
    ):
        raise PluginError(
            "core_state_config_invalid",
            "Supply a database name and bounded application/node identities",
        )
    suffix = hashlib.sha256((database + "\0" + application_id).encode()).hexdigest()[
        :20
    ]
    role, password = f"tf_core_{suffix}", secrets.token_urlsafe(32)
    role_created = database_created = False
    try:
        dsn = make_conninfo(admin_dsn, dbname=database, user=role, password=password)
        values = {
            "TOOLFRAME_CORE_STATE_BACKEND": "postgresql",
            "TOOLFRAME_CORE_DATABASE_URL": dsn,
            "TOOLFRAME_CORE_APPLICATION_ID": application_id,
            "TOOLFRAME_CORE_NODE_ID": node_id,
        }
        if write_environment:
            write_environment(values)
        with pg.connect(admin_dsn, autocommit=True, connect_timeout=5) as admin:
            if admin.info.server_version < 160000:
                raise PluginError(
                    "core_schema_incompatible",
                    "Core PostgreSQL storage requires PostgreSQL 16 or newer",
                )
            if (
                admin.execute(
                    "SELECT 1 FROM pg_database WHERE datname=%s", (database,)
                ).fetchone()
                or admin.execute(
                    "SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)
                ).fetchone()
            ):
                raise PluginError(
                    "core_state_exists", "Core setup requires a new database and role"
                )
            admin.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}"
                ).format(sql.Identifier(role), sql.Literal(password))
            )
            role_created = True
            admin.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(database), sql.Identifier(role)
                )
            )
            database_created = True
            admin.execute(
                sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                    sql.Identifier(database)
                )
            )
        with pg.connect(dsn, connect_timeout=5) as connection:
            connection.execute(SCHEMA_SQL)
            connection.execute(
                "INSERT INTO _toolframe_core.identity(application_id,schema_version) VALUES(%s,%s)",
                (application_id, SCHEMA_VERSION),
            )
        return values
    except BaseException as error:
        if role_created or database_created:
            try:
                with pg.connect(admin_dsn, autocommit=True, connect_timeout=5) as admin:
                    if database_created:
                        admin.execute(
                            sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                                sql.Identifier(database)
                            )
                        )
                    if role_created:
                        admin.execute(
                            sql.SQL("DROP ROLE {}").format(sql.Identifier(role))
                        )
            except pg.Error:
                raise PluginError(
                    "core_state_setup_cleanup_failed",
                    "Core setup failed; inspect its new database and role before retrying",
                ) from None
        if isinstance(error, pg.Error):
            raise PluginError(
                "core_state_setup_failed", "Cannot provision the Core database"
            ) from None
        raise


def inspect_or_recover(settings, *, expected_run=None, confirm=False):
    pg = driver()
    from psycopg.rows import dict_row

    try:
        with pg.connect(
            settings.database_url, row_factory=dict_row, connect_timeout=5
        ) as connection:
            connection.execute("SET LOCAL statement_timeout TO '5s'")
            row = connection.execute(
                "SELECT * FROM _toolframe_core.identity WHERE singleton"
            ).fetchone()
            check_identity(row, settings.application_id)
            if expected_run is None:
                return {key: value for key, value in row.items() if key != "singleton"}
            if not confirm:
                raise PluginError(
                    "core_state_recovery_unconfirmed",
                    "Verify the previous Core and every owned worker stopped, then pass --confirm-workers-stopped",
                )
            if not connection.execute(
                "SELECT pg_try_advisory_xact_lock(%s) AS acquired",
                (lock_key(settings.application_id),),
            ).fetchone()["acquired"]:
                raise PluginError(
                    "host_already_running", "A Core still owns this database"
                )
            row = connection.execute(
                "SELECT * FROM _toolframe_core.identity WHERE singleton FOR UPDATE"
            ).fetchone()
            if row["owner_run"] != expected_run:
                raise PluginError(
                    "stale_plan", "Core ownership changed; inspect it again"
                )
            connection.execute(
                "UPDATE _toolframe_core.identity SET owner_run=NULL, owner_node=NULL WHERE singleton"
            )
            connection.execute(
                "INSERT INTO _toolframe_core.metadata(key,value) VALUES('worker_owners','{}') ON CONFLICT(key) DO UPDATE SET value='{}'"
            )
            return {
                "recovered_run": expected_run,
                "application_id": settings.application_id,
            }
    except pg.Error:
        raise PluginError(
            "core_state_unavailable", "Cannot inspect or recover the Core database"
        ) from None


def run(arguments):
    if arguments.action == "run":
        path = Path(arguments.env_file)
        try:
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW)) as handle:
                info = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_mode & 0o077
                    or info.st_uid != os.getuid()
                ):
                    raise ValueError
                values = json.load(handle)
            if (
                not isinstance(values, dict)
                or set(values) != RUNTIME_KEYS
                or any(
                    not isinstance(value, str) or not value for value in values.values()
                )
            ):
                raise ValueError
        except OSError, ValueError:
            raise PluginError(
                "core_state_config_invalid",
                "Use an owner-controlled mode-0600 Core runtime environment document",
            ) from None
        argv = arguments.argv[1:] if arguments.argv[:1] == ["--"] else arguments.argv
        if not argv:
            raise PluginError("invalid_request", "Supply a command after --")
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"TOOLFRAME_CORE_SETUP_DSN", "TOOLFRAME_CORE_TEST_ADMIN_DSN"}
        }
        environment.update(values)
        os.execvpe(argv[0], argv, environment)
    if arguments.action == "setup":
        destination = Path(arguments.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Reserve the output before making database changes. No credential file is overwritten.
        try:
            descriptor = os.open(
                destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
        except OSError:
            raise PluginError(
                "core_state_output_exists", "Use a new private output file"
            ) from None
        try:
            admin = os.environ.get("TOOLFRAME_CORE_SETUP_DSN")
            if not admin:
                raise PluginError(
                    "core_state_config_invalid",
                    "Set TOOLFRAME_CORE_SETUP_DSN for setup only",
                )
            with os.fdopen(descriptor, "w") as handle:
                descriptor = None

                def persist(values):
                    json.dump(values, handle, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())

                setup(
                    admin,
                    database=arguments.database,
                    application_id=arguments.application_id,
                    node_id=arguments.node_id,
                    write_environment=persist,
                )
        except BaseException as error:
            if descriptor is not None:
                os.close(descriptor)
            if (
                not isinstance(error, PluginError)
                or error.code != "core_state_setup_cleanup_failed"
            ):
                destination.unlink(missing_ok=True)
            raise
        return {"database": arguments.database, "environment_file": str(destination)}
    settings = StateSettings.from_env()
    if settings.backend != "postgresql":
        raise PluginError(
            "core_state_config_invalid",
            "This command requires PostgreSQL Core environment variables",
        )
    return inspect_or_recover(
        settings,
        expected_run=arguments.expected_run if arguments.action == "recover" else None,
        confirm=arguments.confirm_workers_stopped
        if arguments.action == "recover"
        else False,
    )
