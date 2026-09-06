import json
from pathlib import Path
from traceback import format_exception

import pytest
from pydantic import SecretStr, ValidationError
from toolframe_database_config import DatabaseConfigurationError, DatabaseSettings


def test_default_path_and_source_precedence_are_read_only(tmp_path):
    root = tmp_path / "project"
    cwd = tmp_path / "working"
    root.mkdir()
    cwd.mkdir()
    environment = {
        "TOOLFRAME_SQLITE_PATH": "from-sqlite-path.db",
        "TOOLFRAME_DB": "from-db.db",
    }
    original_environment = dict(environment)

    default_settings = DatabaseSettings.from_env(root=root, cwd=cwd, environ={})
    sqlite_environment_settings = DatabaseSettings.from_env(
        root=root,
        cwd=cwd,
        environ={"TOOLFRAME_SQLITE_PATH": environment["TOOLFRAME_SQLITE_PATH"]},
    )
    db_environment_settings = DatabaseSettings.from_env(
        root=root, cwd=cwd, environ={"TOOLFRAME_DB": environment["TOOLFRAME_DB"]}
    )
    cli_settings = DatabaseSettings.from_env(
        root=root,
        cwd=cwd,
        cli_database="from-cli.db",
        environ=environment,
    )

    assert default_settings.sqlite_path == (root / "data.db").resolve()
    assert (
        sqlite_environment_settings.sqlite_path
        == (cwd / "from-sqlite-path.db").resolve()
    )
    assert db_environment_settings.sqlite_path == (cwd / "from-db.db").resolve()
    assert cli_settings.sqlite_path == (cwd / "from-cli.db").resolve()
    assert environment == original_environment
    assert not (root / "data.db").exists()
    assert not (cwd / "from-cli.db").exists()


@pytest.mark.parametrize(
    "source",
    [
        {"TOOLFRAME_SQLITE_PATH": ""},
        {"TOOLFRAME_DB": "   "},
    ],
)
def test_empty_explicit_paths_fail_without_writing(tmp_path, source):
    with pytest.raises(DatabaseConfigurationError):
        DatabaseSettings.from_env(root=tmp_path, cwd=tmp_path, environ=source)
    assert list(tmp_path.iterdir()) == []

    with pytest.raises(DatabaseConfigurationError):
        DatabaseSettings.from_env(
            root=tmp_path,
            cwd=tmp_path,
            cli_database="",
            environ={},
        )


def test_postgresql_and_custom_provider_settings_are_validated(tmp_path):
    dsn = "postgresql+psycopg://user:fake-password@example.test:5433/%E2%9C%93?sslmode=require"
    settings = DatabaseSettings.from_env(
        root=tmp_path,
        cwd=tmp_path,
        environ={
            "TOOLFRAME_DB_PROVIDER": "postgresql",
            "TOOLFRAME_POSTGRES_DSN": dsn,
            "TOOLFRAME_DB_TIMEOUT": "12.5",
            "TOOLFRAME_DB_POOL_SIZE": "8",
            "TOOLFRAME_DB_POOL_TIMEOUT": "17",
            "TOOLFRAME_DB_MAX_ROWS": "2000",
            "TOOLFRAME_DB_MAX_BYTES": "4096",
        },
    )

    assert settings.provider == "postgresql"
    assert settings.timeout_seconds == 12.5
    assert settings.pool_size == 8
    assert settings.pool_timeout_seconds == 17
    assert settings.max_rows == 2000
    assert settings.max_bytes == 4096
    assert settings.public_info()["target"] == "example.test:5433/✓"

    custom = DatabaseSettings(
        provider="package.module:factory",
        sqlite_path=tmp_path / "custom.db",
    )
    assert custom.public_info()["target"] == "configured by custom provider"
    assert (
        DatabaseSettings(
            provider="example_provider:create", sqlite_path=tmp_path / "custom.db"
        ).provider
        == "example_provider:create"
    )

    with pytest.raises(DatabaseConfigurationError):
        DatabaseSettings.from_env(
            root=tmp_path,
            environ={"TOOLFRAME_DB_PROVIDER": "postgresql"},
        )


@pytest.mark.parametrize(
    "provider",
    [
        ":factory",
        "package-module:factory",
        "package.module:1factory",
        "package.é:factory",
    ],
)
def test_invalid_provider_names_fail(tmp_path, provider):
    with pytest.raises(ValidationError):
        DatabaseSettings(provider=provider, sqlite_path=tmp_path / "data.db")


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://user:malicious-secret@/db",
        "postgresql://user:malicious-secret@example.test:bad/db",
        "postgresql://user:malicious-secret@example.test:0/db",
        "postgresql://user:malicious-secret@example.test:5432/",
        "postgresql://user:malicious-secret@example.test/%FF",
        "mysql://user:malicious-secret@example.test/db",
        "postgresql://user:malicious-secret@example.test/db#ignored",
    ],
)
def test_invalid_dsn_errors_are_generic_and_secret_free(tmp_path, dsn):
    with pytest.raises(ValidationError) as raised:
        DatabaseSettings(
            provider="postgresql",
            sqlite_path=tmp_path / "data.db",
            postgres_dsn=dsn,
        )

    exception = raised.value
    rendered = "".join(format_exception(exception))
    assert "malicious-secret" not in str(exception)
    assert "malicious-secret" not in repr(exception)
    assert "malicious-secret" not in rendered
    assert "malicious-secret" not in repr(exception.errors())

    with pytest.raises(DatabaseConfigurationError) as from_env_error:
        DatabaseSettings.from_env(
            root=tmp_path,
            environ={
                "TOOLFRAME_DB_PROVIDER": "postgresql",
                "TOOLFRAME_POSTGRES_DSN": dsn,
            },
        )
    assert "malicious-secret" not in str(from_env_error.value)
    assert from_env_error.value.__cause__ is None
    assert from_env_error.value.__context__ is None


def test_secret_is_absent_from_diagnostics(tmp_path):
    dsn = "postgresql://user:top-secret@example.test:5432/app?sslmode=require"
    settings = DatabaseSettings(
        provider="postgresql",
        sqlite_path=tmp_path / "data.db",
        postgres_dsn=dsn,
    )

    surfaces = (
        repr(settings),
        str(settings),
        json.dumps(settings.model_dump(mode="json")),
        str(settings.public_info()),
    )
    assert all(dsn not in surface for surface in surfaces)
    assert all("top-secret" not in surface for surface in surfaces)
    assert "postgres_dsn" not in settings.public_info()
    assert isinstance(settings.postgres_dsn, SecretStr)


def test_ipv6_diagnostics_keep_host_and_port_unambiguous(tmp_path):
    settings = DatabaseSettings(
        provider="postgresql",
        sqlite_path=tmp_path / "data.db",
        postgres_dsn="postgresql://[::1]:5433/app",
    )
    assert settings.public_info()["target"] == "[::1]:5433/app"


def test_identity_ignores_tuning_and_changes_with_selected_targets(tmp_path):
    sqlite_path = tmp_path / "data.db"
    sqlite = DatabaseSettings(sqlite_path=sqlite_path)
    tuned_sqlite = DatabaseSettings(
        sqlite_path=sqlite_path,
        timeout_seconds=30,
        pool_size=9,
        pool_timeout_seconds=20,
        max_rows=20,
        max_bytes=200,
    )
    other_sqlite = DatabaseSettings(sqlite_path=tmp_path / "other.db")
    assert sqlite.identity == tuned_sqlite.identity
    assert sqlite.identity != other_sqlite.identity

    postgres = DatabaseSettings(
        provider="postgresql",
        sqlite_path=sqlite_path,
        postgres_dsn="postgresql://example.test/app",
    )
    tuned_postgres = DatabaseSettings(
        provider="postgresql",
        sqlite_path=sqlite_path,
        postgres_dsn="postgresql://example.test/app",
        pool_size=10,
    )
    other_postgres = DatabaseSettings(
        provider="postgresql",
        sqlite_path=sqlite_path,
        postgres_dsn="postgresql://other.example.test/app",
    )
    assert postgres.identity == tuned_postgres.identity
    assert postgres.identity != other_postgres.identity

    custom = DatabaseSettings(
        provider="package.module:factory",
        sqlite_path=sqlite_path,
        postgres_dsn="postgresql://example.test/app",
    )
    other_custom = DatabaseSettings(
        provider="package.module:factory",
        sqlite_path=sqlite_path,
        postgres_dsn="postgresql://other.example.test/app",
    )
    assert custom.identity != other_custom.identity


@pytest.mark.parametrize(
    "field_name",
    [
        "timeout_seconds",
        "pool_size",
        "pool_timeout_seconds",
        "max_rows",
        "max_bytes",
    ],
)
def test_direct_boolean_numeric_values_are_rejected(tmp_path, field_name):
    with pytest.raises(ValidationError):
        DatabaseSettings(sqlite_path=tmp_path / "data.db", **{field_name: True})


@pytest.mark.parametrize(
    "environment_name, value",
    [
        ("TOOLFRAME_DB_TIMEOUT", ""),
        ("TOOLFRAME_DB_TIMEOUT", "nan"),
        ("TOOLFRAME_DB_POOL_SIZE", "5.0"),
        ("TOOLFRAME_DB_MAX_ROWS", "nope"),
        ("TOOLFRAME_DB_MAX_BYTES", "0"),
    ],
)
def test_invalid_numeric_environment_values_fail(tmp_path, environment_name, value):
    with pytest.raises(DatabaseConfigurationError):
        DatabaseSettings.from_env(
            root=tmp_path,
            environ={environment_name: value},
        )


def test_model_is_frozen_and_has_forbid_configuration(tmp_path):
    settings = DatabaseSettings(sqlite_path=tmp_path / "data.db")

    with pytest.raises(ValidationError):
        settings.timeout_seconds = 10
    with pytest.raises(ValidationError):
        DatabaseSettings(sqlite_path=tmp_path / "data.db", unexpected="value")

    assert settings.model_config["extra"] == "forbid"
    assert settings.model_config["frozen"] is True
    assert settings.model_config["hide_input_in_errors"] is True
    assert settings.model_config["allow_inf_nan"] is False


def test_sources_are_not_mutated(tmp_path):
    cli_database = Path("relative.db")
    environment = {"TOOLFRAME_DB": "environment.db"}
    original_cli_database = cli_database
    original_environment = dict(environment)

    DatabaseSettings.from_env(
        root=tmp_path,
        cwd=tmp_path,
        cli_database=cli_database,
        environ=environment,
    )

    assert cli_database == original_cli_database
    assert environment == original_environment
