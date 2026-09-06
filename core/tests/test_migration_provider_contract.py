"""Migrations use only the public contract of an independent provider wrapper."""

import sqlite3
import traceback
from contextlib import contextmanager

import pytest
import toolframe_migrations as migrations
from test_database_providers import provider as provider  # noqa: PLC0414
from test_plugin_migrations import rows, schema
from toolframe_database_service import DatabaseProvider
from toolframe_migrations import MigrationCoordinator, MigrationError

SECRET = "test-credential-never-expose-719"


class PublicProvider:
    """Composition only: no private error helper, logger, or implicit forwarding."""

    def __init__(self, backend, *, fail_record=False, fail_normalize=False):
        self.backend = backend
        self.dialect = backend.dialect
        self.identity = backend.identity
        self.settings = backend.settings
        self.fail_record = fail_record
        self.fail_normalize = fail_normalize
        self.connections = 0

    def query(self, *args, **kwargs):
        return self.backend.query(*args, **kwargs)

    def execute(self, *args, **kwargs):
        return self.backend.execute(*args, **kwargs)

    def insert(self, *args, **kwargs):
        return self.backend.insert(*args, **kwargs)

    def transaction(self):
        return self.backend.transaction()

    def run_once(self, *args, **kwargs):
        return self.backend.run_once(*args, **kwargs)

    def public_info(self):
        return self.backend.public_info()

    def close(self):
        self.backend.close()

    def begin_locked(self, connection, key):
        return self.backend.begin_locked(connection, key)

    def normalize_error(self, error, *, operation):
        if self.fail_normalize:
            raise RuntimeError(SECRET)
        return self.backend.normalize_error(error, operation=operation)

    @contextmanager
    def connection(self, *, read_only=False):
        self.connections += 1
        if self.fail_record and self.connections == 3:
            raise RuntimeError(SECRET)
        with self.backend.connection(read_only=read_only) as connection:
            yield connection


@pytest.mark.parametrize("fail_record", [False, True])
def test_sql_failure_and_failure_record_use_public_contract(
    provider, tmp_path, caplog, fail_record
):
    wrapped = PublicProvider(provider, fail_record=fail_record)
    assert isinstance(wrapped, DatabaseProvider)
    declaration = schema(
        tmp_path,
        sql="CREATE TABLE alpha_entries (id INTEGER PRIMARY KEY, value TEXT UNIQUE);"
        f"INSERT INTO alpha_entries VALUES (1,'{SECRET}'),(2,'{SECRET}');",
    )
    with pytest.raises(MigrationError) as raised:
        MigrationCoordinator(wrapped, [declaration]).apply()
    assert raised.value.code == "migration_sql_failed"
    assert not raised.value.retryable
    assert SECRET not in str(raised.value)
    assert SECRET not in "".join(traceback.format_exception(raised.value))
    assert SECRET not in caplog.text
    assert rows(provider, "SELECT status FROM _toolframe_migration_runs") == [
        {"status": "prepared" if fail_record else "failed"}
    ]
    assert rows(provider, "SELECT ref FROM _toolframe_schema_revisions") == []


@pytest.mark.parametrize("kind", ["unknown", "retryable", "normalizer_failure"])
def test_public_normalization_is_safe_and_retains_retryability(
    provider, tmp_path, monkeypatch, caplog, kind
):
    wrapped = PublicProvider(provider, fail_normalize=kind == "normalizer_failure")

    def fail_execution(*args):
        if kind == "retryable":
            if provider.dialect == "sqlite":
                raise sqlite3.OperationalError("database is locked " + SECRET)
            import psycopg

            raise psycopg.OperationalError(SECRET)
        raise RuntimeError(SECRET)

    monkeypatch.setattr(migrations, "execute_script", fail_execution)
    with pytest.raises(MigrationError) as raised:
        MigrationCoordinator(wrapped, [schema(tmp_path)]).apply()
    assert raised.value.code == "migration_sql_failed"
    assert raised.value.retryable is (kind == "retryable")
    assert SECRET not in str(raised.value)
    assert SECRET not in "".join(traceback.format_exception(raised.value))
    assert SECRET not in caplog.text
    assert rows(provider, "SELECT status FROM _toolframe_migration_runs") == [
        {"status": "failed"}
    ]
