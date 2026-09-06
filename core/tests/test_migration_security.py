"""Regression checks for migration write ownership across the complete SQL tree."""

import pytest
from test_database_providers import provider as provider  # noqa: PLC0414
from test_plugin_migrations import schema
from toolframe_migrations import MigrationCoordinator, MigrationError


@pytest.mark.parametrize(
    "statement",
    [
        "WITH changed AS (DELETE FROM beta_entries RETURNING id) SELECT 1;",
        "WITH changed AS (UPDATE beta_entries SET value='changed' RETURNING id) SELECT 1;",
        "WITH changed AS (INSERT INTO beta_entries VALUES (2,'injected') RETURNING id) SELECT 1;",
        "WITH outer_read AS (WITH changed AS (DELETE FROM beta_entries RETURNING id) SELECT 1) SELECT 1;",
        "SELECT id,value INTO beta_snapshot FROM beta_entries;",
    ],
)
def test_read_contract_never_authorizes_nested_or_implicit_writes(
    provider, tmp_path, statement
):
    beta = schema(tmp_path, owner="beta", tables=("beta_entries", "beta_snapshot"))
    MigrationCoordinator(provider, [beta]).apply()
    alpha = schema(
        tmp_path,
        owner="alpha",
        sql=statement,
        depends=("beta@1",),
        reads=("beta_entries", "beta_snapshot"),
    )
    with pytest.raises(MigrationError) as error:
        MigrationCoordinator(provider, [beta, alpha]).apply()
    assert error.value.code == "unsupported_migration_sql"
    assert provider.query("SELECT id,value FROM beta_entries").to_dict("records") == [
        {"id": 1, "value": "first"}
    ]
    with provider.connection(read_only=True) as connection:
        from toolframe_migrations import _table_names

        assert "beta_snapshot" not in _table_names(connection, provider.dialect)
