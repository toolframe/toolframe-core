"""Durable numbering, migration, complete queries and management permissions."""

# ruff: noqa: F811 -- shared host fixture.
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from test_host import fixture_source, host, operate  # noqa: F401
from toolframe_sdk import PluginError

from toolframe_core.access import Identity
from toolframe_core.api import create_app
from toolframe_core.state import CoreState


def test_v1_history_migration_retains_records_and_stable_sequence(tmp_path):
    path = tmp_path / "data/core.sqlite"
    path.parent.mkdir()
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE operations(id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                target TEXT, status TEXT NOT NULL, phase TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, request TEXT NOT NULL,
                result TEXT, error TEXT, idempotency TEXT UNIQUE);
            PRAGMA user_version=1;
        """)
        for identifier, created in (("later", 20), ("first", 10), ("tie", 20)):
            connection.execute(
                "INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    "reload",
                    "retired",
                    "succeeded",
                    "complete",
                    created,
                    30,
                    '{"id":"retired"}',
                    '{"retained":true}',
                    None,
                    identifier,
                ),
            )
    state = CoreState(tmp_path)
    assert [(op["id"], op["sequence"]) for op in state.operations()] == [
        ("tie", 3),
        ("later", 2),
        ("first", 1),
    ]
    assert state.operation("first")["result"] == {"retained": True}
    same, created = state.new_operation("reload", "retired", {"id": "retired"}, "first")
    assert not created and same["sequence"] == 1
    state.close()
    state = CoreState(tmp_path)
    assert state.operation("tie")["sequence"] == 3
    with state.connection:
        state.connection.execute("DELETE FROM operations WHERE id='tie'")
    next_op, _ = state.new_operation("apply", None, {})
    assert next_op["sequence"] == 4  # Never recycle a deleted operation number.
    assert state.connection.execute("PRAGMA user_version").fetchone()[0] == 2
    state.close()


def test_full_history_paging_filters_and_arrivals_do_not_shift_pages(tmp_path):
    state = CoreState(tmp_path)
    for index in range(73):
        op, _ = state.new_operation(
            "reload" if index % 2 else "enable", "retired" if index % 3 else None, {}
        )
        state.update_operation(op["id"], status="failed" if index % 5 else "succeeded")
    first = state.operation_page()
    assert first["total"] == 73 and first["pages"] == 8
    assert [op["sequence"] for op in first["items"]] == list(range(73, 63, -1))
    newest, _ = state.new_operation("validate", "new-plugin", {})
    second = state.operation_page({"page": 2, "through": first["through"]})
    assert second["newer_count"] == 1 and second["total"] == 73
    assert [op["sequence"] for op in second["items"]] == list(range(63, 53, -1))
    last = state.operation_page({"page": 8, "through": first["through"]})
    assert [op["sequence"] for op in last["items"]] == [3, 2, 1]
    filtered = state.operation_page(
        {"target": "retired", "kind": "reload", "status": "succeeded"}
    )
    assert filtered["total"] == 5
    assert all(
        op["target"] == "retired"
        and op["kind"] == "reload"
        and op["status"] == "succeeded"
        for op in filtered["items"]
    )
    assert "new-plugin" in filtered["filters"]["target"]
    assert state.operation_page({"target": ""})["total"] == 25
    empty = state.operation_page({"status": "no-such-result", "page": 99})
    assert empty["items"] == [] and empty["page"] == 1 and empty["total"] == 0
    assert state.operation_page()["items"][0]["id"] == newest["id"]
    state.close()


@pytest.mark.parametrize(
    "params",
    [
        {"page": 0},
        {"page": -1},
        {"page_size": 101},
        {"page_size": 0},
        {"through": -1},
        {"page": "bad"},
        {"unexpected": True},
    ],
)
def test_invalid_history_queries_are_rejected(tmp_path, params):
    state = CoreState(tmp_path)
    try:
        with pytest.raises(PluginError, match="Invalid operation history"):
            state.operation_page(params)
    finally:
        state.close()


async def test_history_api_and_dashboard_bridge_are_read_only_and_authorized(host):
    for _ in range(65):
        host.state.new_operation("reload", "retired", {"management_token": host.token})
    transport = httpx.ASGITransport(
        create_app(host, manage_lifespan=False), client=("192.168.0.9", 1200)
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:9876"
    ) as client:
        assert (await client.get("/_toolframe/v1/operations?page=2")).status_code == 401
        headers = {"Authorization": "Bearer " + host.token}
        legacy = await client.get("/_toolframe/v1/operations", headers=headers)
        assert len(legacy.json()) == 50
        response = await client.get(
            "/_toolframe/v1/operations?page=7&page_size=10&target=retired",
            headers=headers,
        )
        assert response.status_code == 200
        page = response.json()
        assert page["total"] == 65 and len(page["items"]) == 5
        assert host.token not in response.text
        assert (
            await client.get(
                "/_toolframe/v1/operations?page_size=1000", headers=headers
            )
        ).status_code == 422
    dashboard = SimpleNamespace(
        name="dashboard", candidate={"definition": {"provider_kind": "dashboard"}}
    )
    host.runtimes[dashboard.name] = dashboard
    reference = host.access.register(Identity(local=True))
    before = host.state.operations(limit=100)
    try:
        result = await host.worker_call(
            dashboard,
            "management.call",
            {
                "action": "operations",
                "params": {"page": 7, "target": "retired"},
                "access_context": reference,
            },
        )
        assert result == page
        assert host.token not in json.dumps(result)
        assert host.state.operations(limit=100) == before
        with pytest.raises(PluginError):
            await host.worker_call(
                dashboard,
                "management.call",
                {"action": "operations", "access_context": "forged"},
            )
    finally:
        host.runtimes.pop(dashboard.name)
        host.access.contexts.pop(reference)


async def test_dependency_graph_tracks_installation_not_source_creation(host, tmp_path):
    source = fixture_source(tmp_path, "new-graph-plugin")
    assert host.status()["dependency_graph"]["nodes"] == []
    added = await operate(host, "add", source=str(source))
    assert added["status"] == "succeeded", added
    nodes = host.status()["dependency_graph"]["nodes"]
    assert nodes[0]["id"] == "new-graph-plugin" and nodes[0]["state"] == "disabled"
    assert (await operate(host, "enable", id="new-graph-plugin"))[
        "status"
    ] == "succeeded"
    assert host.status()["dependency_graph"]["nodes"][0]["healthy"]
