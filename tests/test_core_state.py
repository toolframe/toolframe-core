"""Exercise the same journal contract against SQLite and real PostgreSQL."""

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import pytest
import tomlkit
from toolframe_sdk import PluginError

from toolframe_core.api import create_app
from toolframe_core.config import ConfigFile, atomic_write
from toolframe_core.host import Host
from toolframe_core.state import CoreState, StateSettings
from toolframe_core.state_admin import inspect_or_recover, setup


@dataclass(repr=False)
class Database:
    settings: StateSettings
    admin_dsn: str


@pytest.fixture
def postgres_database():
    admin = os.environ.get("TOOLFRAME_CORE_TEST_ADMIN_DSN")
    if not admin:
        pytest.skip("Set TOOLFRAME_CORE_TEST_ADMIN_DSN for PostgreSQL integration")
    pg = pytest.importorskip("psycopg")
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict

    name = "tf_core_test_" + uuid.uuid4().hex[:16]
    application = str(uuid.uuid4())
    values = setup(
        admin, database=name, application_id=application, node_id="test-node"
    )
    settings = StateSettings(
        "postgresql", values["TOOLFRAME_CORE_DATABASE_URL"], application, "test-node"
    )
    try:
        yield Database(settings, admin)
    finally:
        with pg.connect(admin, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )
            connection.execute(
                sql.SQL("DROP ROLE {}").format(
                    sql.Identifier(conninfo_to_dict(settings.database_url)["user"])
                )
            )


@pytest.fixture(params=["sqlite", "postgresql"])
async def journal(request, tmp_path):
    settings = (
        StateSettings()
        if request.param == "sqlite"
        else request.getfixturevalue("postgres_database").settings
    )
    state = await CoreState.open(tmp_path, settings=settings)
    try:
        yield state
    finally:
        await state.close()


async def test_journal_contract_and_restart(journal, tmp_path):
    await journal.set_meta("example", {"value": 123})
    assert await journal.meta("example") == {"value": 123}
    assert await journal.meta("absent", "default") == "default"
    for identifier in ("old", "new"):
        await journal.save_candidate(
            {
                "id": identifier,
                "plugin_id": "alpha",
                "definition": {
                    "config_schema": {"properties": {"opaque": {"writeOnly": True}}}
                },
                "config": {"opaque": "private-example"},
            }
        )
        await journal.select("alpha", identifier)
    await journal.stopped("alpha", True)
    await journal.runtime("alpha", {"state": "active"})
    plugin = await journal.plugin("alpha")
    assert (
        plugin["current_candidate"] == "new" and plugin["previous_candidate"] == "old"
    )
    assert plugin["stopped"]
    assert "private-example" in journal.secrets
    op, created = await journal.new_operation(
        "reload", "alpha", {"id": "alpha"}, "retry-key"
    )
    assert created
    same, created = await journal.new_operation(
        "reload", "alpha", {"id": "alpha"}, "retry-key"
    )
    assert not created and same == op
    with pytest.raises(PluginError, match="Key already belongs"):
        await journal.new_operation("reload", "alpha", {"id": "different"}, "retry-key")
    await journal.update_operation(op["id"], status="running", phase="activating")
    settings = journal.settings
    await journal.close()
    reopened = await CoreState.open(tmp_path, settings=settings)
    try:
        await reopened.reconcile_startup("new-host-run")
        assert (await reopened.operation(op["id"]))["status"] == "interrupted"
        restored = await reopened.plugin("alpha")
        assert restored["runtime"]["previous_observation"] == {"state": "active"}
        assert restored["runtime"]["host_run"] == "new-host-run"
        assert not restored["stopped"]
        assert await reopened.meta("example") == {"value": 123}
        assert "private-example" in reopened.secrets
    finally:
        await reopened.close()


async def test_concurrent_idempotency_and_stable_history(journal):
    values = await asyncio.gather(
        *(journal.new_operation("reload", "alpha", {}, "same") for _ in range(12))
    )
    assert len({op["id"] for op, _ in values}) == 1
    assert sum(created for _, created in values) == 1
    for index in range(23):
        op, _ = await journal.new_operation(
            "reload" if index % 2 else "apply", "alpha" if index % 3 else None, {}
        )
        await journal.update_operation(
            op["id"], status="succeeded", result={"index": index}
        )
    first = await journal.operation_page({"page_size": 10})
    await journal.new_operation("plan", "new", {})
    second = await journal.operation_page({"page": 2, "through": first["through"]})
    assert first["total"] == second["total"] == 24
    assert second["newer_count"] == 1
    assert not {v["id"] for v in first["items"]} & {v["id"] for v in second["items"]}
    assert (await journal.operation_page({"target": ""}))["total"] == 8
    with pytest.raises(PluginError, match="Invalid operation history"):
        await journal.operation_page({"page_size": 0})


async def test_secret_cache_preserves_history_numbers_and_schema_metadata(
    journal, tmp_path
):
    from toolframe_core.redaction import redact

    candidate = {
        "id": "configured",
        "plugin_id": "alpha",
        "definition": {
            "config_schema": {
                "properties": {
                    "password": {"type": "string", "minLength": 1},
                    "opaque": {"writeOnly": True},
                }
            }
        },
        "config": {"password": "private-password", "opaque": "private-opaque"},
    }
    await journal.save_candidate(candidate)
    # Nested journal values must also distinguish definitions from configuration.
    await journal.set_meta("nested", {"candidate": candidate})
    await journal.new_operation("validate", "alpha", {"token": "private-request"})
    settings = journal.settings
    async def assert_history(state):
        page = redact(await state.operation_page(), state.secrets)
        assert page["page"] == page["total"] == page["pages"] == 1
        assert redact("private-opaque", state.secrets) == "[REDACTED]"
        assert redact("private-password", state.secrets) == "[REDACTED]"
        assert page["items"][0]["request"]["token"] == "[REDACTED]"
        assert "string" not in state.secrets and "1" not in state.secrets

    await assert_history(journal)
    await journal.close()
    reopened = await CoreState.open(tmp_path, settings=settings)
    try:
        await assert_history(reopened)
        assert "private-request" in reopened.secrets
    finally:
        await reopened.close()


async def test_single_owner_and_explicit_crash_recovery(postgres_database, tmp_path):
    settings = postgres_database.settings
    state = await CoreState.open(tmp_path, settings=settings)
    try:
        with pytest.raises(PluginError) as error:
            await CoreState.open(tmp_path / "other", settings=settings)
        assert error.value.code == "host_already_running"
        row = inspect_or_recover(settings)
        with pytest.raises(PluginError) as error:
            inspect_or_recover(settings, expected_run=row["owner_run"], confirm=True)
        assert error.value.code == "host_already_running"
        await state.new_operation("apply", None, {}, "survive")
    finally:
        await state.close(clean=False)
    with pytest.raises(PluginError) as error:
        await CoreState.open(tmp_path, settings=settings)
    assert error.value.code == "core_state_recovery_required"
    with pytest.raises(PluginError) as error:
        inspect_or_recover(settings, expected_run=row["owner_run"], confirm=False)
    assert error.value.code == "core_state_recovery_unconfirmed"
    with pytest.raises(PluginError) as error:
        inspect_or_recover(settings, expected_run="stale", confirm=True)
    assert error.value.code == "stale_plan"
    inspect_or_recover(settings, expected_run=row["owner_run"], confirm=True)
    state = await CoreState.open(tmp_path, settings=settings)
    try:
        op, created = await state.new_operation("apply", None, {}, "survive")
        assert not created
        await state.reconcile_startup("recovered")
        assert (await state.operation(op["id"]))["status"] == "interrupted"
    finally:
        await state.close()


async def test_cancelled_query_poisoning_and_nonblocking_io(
    postgres_database, tmp_path, monkeypatch
):
    state = await CoreState.open(tmp_path, settings=postgres_database.settings)
    entered = asyncio.Event()
    original = state.backend.meta

    async def slow_meta(*args):
        entered.set()
        await state.backend.connection.execute("SELECT pg_sleep(2)")
        return await original(*args)

    monkeypatch.setattr(state.backend, "meta", slow_meta)
    task = asyncio.create_task(state.meta("slow"))
    await entered.wait()
    await asyncio.sleep(0.03)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state.failed
    with pytest.raises(PluginError) as error:
        await state.set_meta("must-not-write", 1)
    assert error.value.code == "core_state_unavailable"
    await state.close()
    assert inspect_or_recover(postgres_database.settings)["owner_run"] is not None


async def test_connection_loss_and_unknown_commit_reconcile(
    postgres_database, tmp_path, monkeypatch
):
    import psycopg

    settings = postgres_database.settings
    state = await CoreState.open(tmp_path, settings=settings)
    original = state.backend.connection.transaction

    @asynccontextmanager
    async def lost_acknowledgement():
        async with original():
            yield
        raise psycopg.OperationalError("simulated lost commit acknowledgement")

    monkeypatch.setattr(state.backend.connection, "transaction", lost_acknowledgement)
    with pytest.raises(PluginError) as error:
        await state.new_operation("reload", "alpha", {}, "committed")
    assert error.value.code == "core_state_unavailable" and state.failed
    await state.close()
    row = inspect_or_recover(settings)
    inspect_or_recover(settings, expected_run=row["owner_run"], confirm=True)
    state = await CoreState.open(tmp_path, settings=settings)
    try:
        op, created = await state.new_operation("reload", "alpha", {}, "committed")
        assert not created and op["sequence"] == 1
        with psycopg.connect(postgres_database.admin_dsn, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(%s)",
                (state.backend.connection.info.backend_pid,),
            )
        with pytest.raises(PluginError) as error:
            await state.meta("after-disconnect")
        assert error.value.code == "core_state_unavailable"
        assert settings.database_url not in str(error.value)
        assert not (tmp_path / "data/core.sqlite").exists()
    finally:
        await state.close()


async def test_failed_multi_statement_write_rolls_back(
    postgres_database, tmp_path, monkeypatch
):
    state = await CoreState.open(tmp_path, settings=postgres_database.settings)

    async def fail(_):
        raise PluginError("test_failure", "injected transaction failure")

    monkeypatch.setattr(state.backend, "_ensure_plugin", fail)
    try:
        with pytest.raises(PluginError, match="injected transaction failure"):
            await state.save_candidate({"id": "rolled-back", "plugin_id": "alpha"})
        with pytest.raises(PluginError) as error:
            await state.candidate("rolled-back")
        assert error.value.code == "candidate_missing"
        assert not state.failed
    finally:
        await state.close()


async def test_query_timeout_invalidates_owner(
    postgres_database, tmp_path, monkeypatch
):
    state = await CoreState.open(tmp_path, settings=postgres_database.settings)
    await state.backend.connection.execute("SET statement_timeout TO '20ms'")

    async def slow_meta(*args):
        await state.backend.connection.execute("SELECT pg_sleep(2)")

    monkeypatch.setattr(state.backend, "meta", slow_meta)
    try:
        with pytest.raises(PluginError) as error:
            await state.meta("slow")
        assert error.value.code == "core_state_unavailable" and state.failed
    finally:
        await state.close()


async def test_host_disposes_workers_after_database_disconnect(
    postgres_database, tmp_path
):
    from pathlib import Path

    import psycopg
    from test_host import fixture_source

    from toolframe_core.config import PluginRequest

    settings = postgres_database.settings
    source = fixture_source(tmp_path, "example")
    home = tmp_path / "application"
    config = ConfigFile(home)
    composition = config.initialize()
    composition.application.id = settings.application_id
    composition.plugins["example"] = PluginRequest(source=str(source), enabled=True)
    composition.packages.find_links = [
        str(Path(__file__).resolve().parents[2] / "toolframe-sdk/dist")
    ]
    atomic_write(config.path, tomlkit.dumps(composition.model_dump()))
    host = await Host.create(home, state_settings=settings)
    try:
        await host.start()
        worker = host.runtimes["example"].worker
        assert worker.process.returncode is None
        with psycopg.connect(postgres_database.admin_dsn, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(%s)",
                (host.state.backend.connection.info.backend_pid,),
            )
        async with asyncio.timeout(10):
            while not host.closing or worker.process.returncode is None:
                await asyncio.sleep(0.05)
        assert not host.healthy_runtime("example")
        with pytest.raises(PluginError) as error:
            await host.submit("apply", {})
        assert error.value.code == "core_state_unavailable"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(create_app(host, manage_lifespan=False)),
            base_url="http://127.0.0.1:9876",
        ) as client:
            assert (await client.get("/healthz")).status_code == 200
            assert (await client.get("/_toolframe/v1/status")).status_code == 503
    finally:
        await host.close()
    assert inspect_or_recover(settings)["owner_run"] is not None


async def test_postgres_host_api_restart_and_scratch_guard(postgres_database, tmp_path):
    settings = postgres_database.settings
    config = ConfigFile(tmp_path)
    composition = config.initialize()
    composition.application.id = settings.application_id
    atomic_write(config.path, tomlkit.dumps(composition.model_dump()))
    host = await Host.create(tmp_path, state_settings=settings)
    await host.start()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(create_app(host, manage_lifespan=False)),
            base_url="http://127.0.0.1:9876",
        ) as client:
            assert (await client.get("/_toolframe/v1/status")).status_code == 200
            response = await client.post(
                "/_toolframe/v1/operations",
                json={"action": "plan", "params": {}},
                headers={"idempotency-key": "api-retry"},
            )
            assert response.status_code == 202
            identifier = response.json()["id"]
            assert (await host.wait_operation(identifier))["status"] == "succeeded"
            assert (await client.get("/_toolframe/v1/operations?page=1")).json()[
                "total"
            ] == 2
        with pytest.raises(PluginError) as error:
            await host.runtime_validate("anything", {})
        assert error.value.code == "runtime_validation_unsupported"
    finally:
        await host.close()
    (tmp_path / "toolframe.toml").write_text("invalid = [")
    host = await Host.create(tmp_path, state_settings=settings)
    try:
        await host.start()
        assert (await host.state.operation(identifier))["status"] == "succeeded"
        assert (await host.status())["configuration_error"]
    finally:
        await host.close()
    assert not (tmp_path / "data/core.sqlite").exists()


async def test_wrong_application_and_existing_sqlite_are_rejected(
    postgres_database, tmp_path
):
    settings = postgres_database.settings
    wrong = StateSettings(
        "postgresql", settings.database_url, "another-app", settings.node_id
    )
    with pytest.raises(PluginError) as error:
        await CoreState.open(tmp_path, settings=wrong)
    assert error.value.code == "core_state_identity_mismatch"
    sqlite = await CoreState.open(tmp_path, settings=StateSettings())
    await sqlite.close()
    with pytest.raises(PluginError) as error:
        await CoreState.open(tmp_path, settings=settings)
    assert error.value.code == "core_state_migration_required"


def test_runtime_configuration_masks_credentials(monkeypatch):
    monkeypatch.setenv("TOOLFRAME_CORE_STATE_BACKEND", "postgresql")
    monkeypatch.setenv(
        "TOOLFRAME_CORE_DATABASE_URL", "postgresql://secret-password@invalid/"
    )
    monkeypatch.delenv("TOOLFRAME_CORE_APPLICATION_ID", raising=False)
    monkeypatch.delenv("TOOLFRAME_CORE_NODE_ID", raising=False)
    with pytest.raises(PluginError) as error:
        StateSettings.from_env()
    assert "secret-password" not in str(error.value)


async def test_schema_version_and_privileged_runtime_are_refused(
    postgres_database, tmp_path
):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    settings = postgres_database.settings
    privileged = StateSettings(
        "postgresql",
        make_conninfo(
            postgres_database.admin_dsn,
            dbname=conninfo_to_dict(settings.database_url)["dbname"],
        ),
        settings.application_id,
        settings.node_id,
    )
    with pytest.raises(PluginError) as error:
        await CoreState.open(tmp_path, settings=privileged)
    assert error.value.code == "core_state_role_invalid"
    with psycopg.connect(settings.database_url) as connection:
        connection.execute("UPDATE _toolframe_core.identity SET schema_version=999")
    with pytest.raises(PluginError) as error:
        await CoreState.open(tmp_path, settings=settings)
    assert error.value.code == "core_schema_incompatible"


def test_provisioning_failure_cleans_only_new_resources(postgres_database, monkeypatch):
    import hashlib

    import psycopg

    from toolframe_core import state_admin

    database = "tf_core_test_" + uuid.uuid4().hex[:16]
    application = str(uuid.uuid4())
    role = (
        "tf_core_"
        + hashlib.sha256((database + "\0" + application).encode()).hexdigest()[:20]
    )
    monkeypatch.setattr(state_admin, "SCHEMA_SQL", "INVALID SQL;")
    with pytest.raises(PluginError) as error:
        setup(
            postgres_database.admin_dsn,
            database=database,
            application_id=application,
            node_id="test-node",
        )
    assert error.value.code == "core_state_setup_failed"
    with psycopg.connect(postgres_database.admin_dsn) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM pg_database WHERE datname=%s", (database,)
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)
            ).fetchone()
            is None
        )
    assert (
        inspect_or_recover(postgres_database.settings)["application_id"]
        == postgres_database.settings.application_id
    )


def test_environment_persistence_failure_precedes_provisioning(monkeypatch):
    psycopg = pytest.importorskip("psycopg")

    def must_not_connect(*args, **kwargs):
        pytest.fail("Provisioning ran before credentials could be persisted")

    def full_disk(_):
        raise OSError("simulated full disk")

    monkeypatch.setattr(psycopg, "connect", must_not_connect)
    with pytest.raises(OSError, match="simulated full disk"):
        setup(
            "dbname=postgres",
            database="tf_core_test_failure",
            application_id="test-app",
            node_id="test-node",
            write_environment=full_disk,
        )


def test_private_setup_output_and_no_overwrite(postgres_database, tmp_path):
    from argparse import Namespace

    from toolframe_core.state_admin import run

    output = tmp_path / "environment.json"
    output.write_text("retained")
    with pytest.raises(PluginError) as error:
        run(Namespace(action="setup", output=str(output)))
    assert error.value.code == "core_state_output_exists"
    assert output.read_text() == "retained"
