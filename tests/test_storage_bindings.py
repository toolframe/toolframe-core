"""SQL provider selection, retained ownership, and admitted call routing."""

import asyncio
import copy
import time
from contextlib import ExitStack
from types import SimpleNamespace

import pytest
from toolframe_sdk import (
    AuthorizationSpec,
    MethodSpec,
    Migration,
    PluginDefinition,
    PluginError,
    Requirement,
    ServiceSpec,
    StoreSchema,
)

from toolframe_core.graph import Graph
from toolframe_core.host import Host, Root
from toolframe_core.stores import RetainedStores

SQL = "toolframe.sql"


def candidate(name, *, provider=None, requirements=None, store=False):
    definition = PluginDefinition(
        id=name,
        factory="fixture:Example",
        kind="provider" if provider else "business",
        provider_kind=provider,
        authorization=AuthorizationSpec(),
        requires=requirements or {},
        services={SQL: ServiceSpec(methods={"query": MethodSpec(audience="internal")})}
        if provider == "sql"
        else {},
        store=StoreSchema(
            revision="v1", migrations=(Migration(revision="v1", sql="SELECT 1;"),)
        )
        if store
        else None,
    )
    return {"id": name, "definition": definition.model_dump()}


def composition():
    return {
        "sqlite": candidate("sqlite", provider="sql"),
        "postgres": candidate("postgres", provider="sql"),
        **{
            name: candidate(
                name, requirements={"sql": Requirement(service=SQL)}, store=True
            )
            for name in ("alpha", "beta")
        },
    }


def test_multiple_sql_providers_use_default_and_per_consumer_bindings():
    graph = Graph(composition(), {SQL: "postgres", "alpha/sql": "sqlite"})
    assert graph.sql_providers == {"sqlite", "postgres"}
    assert graph.stores == {"alpha": "sqlite", "beta": "postgres"}
    assert graph.dependencies["alpha"] == {"sqlite"}
    assert graph.dependencies["beta"] == {"postgres"}
    assert graph.dependents({"postgres"}) == {"postgres", "beta"}


def test_single_provider_still_resolves_without_configuration():
    values = composition()
    del values["postgres"]
    graph = Graph(values, {})
    assert graph.stores == {"alpha": "sqlite", "beta": "sqlite"}


def test_multiple_unbound_providers_are_ambiguous():
    with pytest.raises(PluginError) as error:
        Graph(composition(), {})
    assert error.value.code == "service_ambiguous"


def test_disabled_explicit_provider_never_falls_back_to_the_other_database():
    values = composition()
    del values["postgres"]
    graph = Graph(values, {SQL: "postgres", "alpha/sql": "sqlite"})
    assert graph.stores == {"alpha": "sqlite"}
    assert graph.missing["beta"] == [SQL]


@pytest.mark.parametrize("optional", [False, True])
def test_two_aliases_cannot_assign_one_plugin_to_two_stores(optional):
    values = composition()
    values["alpha"] = candidate(
        "alpha",
        store=True,
        requirements={
            "sql": Requirement(service=SQL),
            "archive": Requirement(service=SQL, optional=optional),
        },
    )
    with pytest.raises(PluginError) as error:
        Graph(values, {SQL: "postgres", "alpha/archive": "sqlite"})
    assert error.value.code == "multiple_store_providers"
    assert Graph(values, {SQL: "postgres"}).stores["alpha"] == "postgres"


@pytest.mark.parametrize(
    "requirements", [{}, {"sql": Requirement(service=SQL, optional=True)}]
)
def test_managed_store_requires_a_required_sql_dependency(requirements):
    with pytest.raises(PluginError) as error:
        Graph({"alpha": candidate("alpha", store=True, requirements=requirements)}, {})
    assert error.value.code == "invalid_store_dependency"


def test_sql_dependency_must_resolve_to_a_storage_provider():
    values = composition()
    values["postgres"]["definition"].update(kind="business", provider_kind=None)
    with pytest.raises(PluginError) as error:
        Graph(values, {SQL: "postgres"})
    assert error.value.code == "invalid_sql_provider"


def test_route_providers_still_have_one_owner():
    with pytest.raises(PluginError) as error:
        Graph(
            {
                name: candidate(name, provider="dashboard")
                for name in ("first", "second")
            },
            {},
        )
    assert error.value.code == "provider_conflict"


class Journal:
    def __init__(self, metadata=None, candidates=None):
        self.metadata = metadata or {}
        self.candidates = candidates or {}

    async def meta(self, key, default=None):
        return copy.deepcopy(self.metadata.get(key, default))

    async def set_meta(self, key, value):
        self.metadata[key] = copy.deepcopy(value)

    async def plugins(self):
        return [{"id": name, "current_candidate": name} for name in self.candidates]

    async def candidate(self, key):
        return self.candidates[key]


async def test_retained_store_survives_restart_and_blocks_rebinding():
    state = Journal()
    stores = await RetainedStores.load(state)
    await stores.remember("alpha", "sqlite")
    restarted = await RetainedStores.load(state)
    restarted.check("alpha", "sqlite")
    with pytest.raises(PluginError) as error:
        restarted.check("alpha", "postgres")
    assert error.value.code == "store_rebind_required"
    graph = Graph(composition(), {SQL: "postgres"})
    assert restarted.describe(graph)["alpha"] == {
        "provider": "postgres",
        "retained_provider": "sqlite",
        "rebind_required": True,
    }


async def test_upgrade_adopts_recorded_single_provider_before_new_config():
    values = composition()
    previous = {
        "plugins": {"sqlite": {"enabled": True}, "postgres": {"enabled": False}},
        "bindings": {},
    }
    stores = await RetainedStores.load(
        Journal({"last_valid_composition": previous}, values)
    )
    assert stores.bindings == {"alpha": "sqlite", "beta": "sqlite"}
    with pytest.raises(PluginError, match="Retained stores"):
        stores.check("alpha", "postgres")


async def test_nested_calls_use_the_admitted_binding_snapshot():
    host = Host()
    caller = SimpleNamespace(name="alpha")
    host.runtimes = {"alpha": caller}
    root = Root(
        "root",
        "alpha",
        time.time() + 10,
        host.runtimes,
        ExitStack(),
        paths={("alpha",): 1},
        bindings={"alpha": {"sql": ("sqlite", SQL)}},
    )
    host.roots = {root.id: root}
    host.graph = SimpleNamespace(bindings={"alpha": {"sql": ("postgres", SQL)}})

    async def invoke(root, target, service, method, payload, **kwargs):
        return target

    host.invoke = invoke
    assert (
        await host.worker_call(
            caller,
            "service.call",
            {
                "root_id": root.id,
                "chain": ["alpha"],
                "alias": "sql",
                "method": "query",
            },
        )
        == "sqlite"
    )


async def test_root_cleanup_attempts_every_provider_and_can_retry():
    host = Host()
    runtimes = {
        name: SimpleNamespace(
            name=name,
            candidate={"definition": {"provider_kind": "sql"}},
            worker=SimpleNamespace(process=SimpleNamespace(returncode=None)),
        )
        for name in ("sqlite", "postgres")
    }
    root = Root("root", "alpha", time.time() + 10, runtimes, ExitStack())
    released = []
    root.leases.callback(released.append, True)
    host.roots = {root.id: root}
    host.failed_roots, host.cleanup_blocked, host.gated = set(), set(), set()
    host.changed = asyncio.Event()
    calls = []

    async def admin(runtime, method, params):
        calls.append(runtime.name)
        if calls == ["sqlite"]:
            raise PluginError("cleanup_failed", "First provider disconnected")

    host.admin = admin
    with pytest.raises(PluginError, match="First provider"):
        await host.end_root(root)
    assert calls == ["sqlite", "postgres"]
    assert root.id in host.failed_roots and not released
    await host.end_root(root)
    assert calls == ["sqlite", "postgres", "sqlite", "postgres"]
    assert released == [True] and not host.roots and not host.failed_roots
