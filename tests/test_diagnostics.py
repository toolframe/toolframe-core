from copy import deepcopy

from toolframe_sdk import (
    AuthorizationSpec,
    MethodSpec,
    PluginDefinition,
    Requirement,
    Resource,
    ServiceSpec,
)

from toolframe_core.diagnostics import dependency_snapshot
from toolframe_core.graph import Graph


def candidate(name, *, requires=None, provider=None, resources=(), version="1.0.0"):
    definition = PluginDefinition(
        id=name,
        factory="fixture:Plugin",
        kind="provider" if provider else "business",
        provider_kind=provider,
        authorization=AuthorizationSpec(),
        services={
            f"{name}.service": ServiceSpec(
                methods={"read": MethodSpec(audience="internal", read_only=True)}
            )
        },
        requires=requires or {},
        resources=resources,
    )
    return {
        "id": f"{name}-{version}",
        "version": version,
        "definition": definition.model_dump(mode="json"),
        "config": {"password": "never-publish-this"},
    }


def page():
    return Resource(
        id="main",
        kind="dashboard.page",
        required_scopes=("system.plugins.read",),
        data={"entrypoint": "fixture:page"},
    )


def row(name, *, enabled=True, healthy=True, state="active"):
    return {
        "id": name,
        "desired": {"enabled": enabled},
        "runtime": {"state": state},
        "stopped_override": False,
        "blocked_by": [],
        "healthy": healthy,
        "health_fresh": healthy,
    }


def snapshot(graph, *, known=None, rows=None, bindings=None, cordis=()):
    known = known or graph.candidates
    return dependency_snapshot(
        graph,
        rows or [row(name) for name in sorted(known)],
        known,
        bindings or {},
        cordis,
        {"dashboard": {"state": "available"}},
    )


def test_impacts_use_planner_and_separate_optional_and_shared_presentations():
    candidates = {
        "sql": candidate("sql", provider="sql"),
        "auth": candidate("auth", requires={"db": Requirement(service="sql.service")}),
        "passwords": candidate(
            "passwords",
            requires={"auth": Requirement(service="auth.service")},
            resources=(page(),),
        ),
        "dashboard": candidate("dashboard", provider="dashboard"),
        "optional": candidate(
            "optional",
            requires={"db": Requirement(service="sql.service", optional=True)},
        ),
        "optional-auth": candidate(
            "optional-auth",
            requires={"auth": Requirement(service="auth.service", optional=True)},
        ),
        "unrelated": candidate("unrelated", resources=(page(),)),
    }
    graph = Graph(candidates, {})
    before = deepcopy(candidates)
    report = snapshot(graph)
    assert candidates == before  # A diagnostic cannot prepare or modify candidates.
    assert "never-publish-this" not in str(report)
    for action in ("reload", "disable"):
        result = report["impacts"]["sql"][action]
        replacement = (
            graph
            if action == "reload"
            else Graph(
                {name: value for name, value in candidates.items() if name != "sql"},
                {},
            )
        )
        assert result["affected"] == sorted(graph.affected(replacement, {"sql"}))
        assert result["affected"] == ["auth", "dashboard", "passwords", "sql"]
        assert result["required_dependents"] == ["auth", "passwords"]
        assert result["presentation_providers"] == ["dashboard"]
        assert result["optional_consumers"] == ["optional", "optional-auth"]
        # Another page reconnects with the shared UI, without restarting its service.
        assert {item["owner"] for item in result["presentations"]} == {
            "passwords",
            "unrelated",
        }
    assert {edge["kind"] for edge in report["edges"]} == {
        "required",
        "optional",
        "presentation",
    }


def test_selected_definition_wins_over_new_candidate_and_cordis_supplies_state():
    old = candidate("app")
    graph = Graph({"app": old}, {})
    new = candidate(
        "app", requires={"new": Requirement(service="new.service")}, version="2.0.0"
    )
    report = snapshot(
        graph,
        known={"app": new},
        rows=[row("app", healthy=False)],
        cordis=[{"name": "app", "state": "draining", "inFlight": 3, "blockedBy": []}],
    )
    assert report["edges"] == []
    node = report["nodes"][0]
    assert (node["version"], node["state"], node["in_flight"]) == (
        "1.0.0",
        "draining",
        3,
    )
    assert node["healthy"] is False
    assert node["definition_source"] == "selected"


def test_disabled_supplier_missing_optional_and_absent_presentation_are_visible():
    known = {
        "sql": candidate("sql", provider="sql"),
        "app": candidate(
            "app",
            resources=(page(),),
            requires={
                "store": Requirement(service="sql.service"),
                "optional": Requirement(service="remote.service", optional=True),
            },
        ),
        "disabled-page": candidate("disabled-page", resources=(page(),)),
    }
    graph = Graph({"app": known["app"]}, {})
    report = snapshot(
        graph,
        known=known,
        rows=[
            row("app", healthy=False, state="blocked"),
            row("sql", enabled=False, healthy=False),
            row("disabled-page", enabled=False, healthy=False),
        ],
    )
    nodes = {node["id"]: node for node in report["nodes"]}
    assert nodes["sql"]["state"] == "disabled"
    assert nodes["sql"]["definition_source"] == "last_candidate"
    assert nodes["requirement:app:optional"]["state"] == "missing"
    assert nodes["provider:dashboard"]["state"] == "missing"
    assert len(nodes) == len(report["nodes"])
    assert set(report["impacts"]) == {"app"}
    assert any(
        edge["target"] == "sql" and edge["state"] == "missing"
        for edge in report["edges"]
    )
    assert any(
        edge["source"] == "disabled-page" and edge["state"] == "inactive"
        for edge in report["edges"]
    )


def test_explicit_binding_and_version_mismatch_do_not_guess_a_supplier():
    first = candidate("first")
    second = candidate("second")
    second["definition"]["services"] = first["definition"]["services"]
    app = candidate(
        "app",
        requires={
            "chosen": Requirement(service="first.service"),
            "version": Requirement(service="first.service", version=2),
        },
    )
    known = {"first": first, "second": second, "app": app}
    bindings = {"app/chosen": "second"}
    report = snapshot(Graph(known, bindings), bindings=bindings)
    edges = {edge["alias"]: edge for edge in report["edges"]}
    assert edges["chosen"]["target"] == "second"
    assert edges["chosen"]["state"] == "resolved"
    assert edges["version"]["target"] == "requirement:app:version"
    assert edges["version"]["state"] == "missing"


def test_inactive_ambiguous_and_unknown_definitions_and_empty_graph():
    first = candidate("first")
    second = candidate("second")
    second["definition"]["services"] = first["definition"]["services"]
    known = {
        "first": first,
        "second": second,
        "app": candidate("app", requires={"db": Requirement(service="first.service")}),
    }
    report = snapshot(
        Graph({}, {}),
        known=known,
        rows=[
            *(row(name, enabled=False, healthy=False) for name in known),
            row("not-installed", enabled=False, healthy=False),
        ],
    )
    nodes = {node["id"]: node for node in report["nodes"]}
    assert nodes["requirement:app:db"]["state"] == "ambiguous"
    assert nodes["not-installed"]["definition_source"] == "unavailable"
    assert not report["impacts"]
    assert snapshot(Graph({}, {}))["nodes"] == []


def test_mcp_restart_only_on_descriptor_removal_and_docs_refresh_without_restart():
    tool = Resource(
        id="read", kind="mcp.tool", data={"service": "app.service", "method": "read"}
    )
    document = Resource(
        id="guide",
        kind="llms.document",
        data={"package": "fixture", "path": "guide.md"},
    )
    app = candidate("app", resources=(tool, document))
    app["definition"]["authorization"]["scopes"] = {
        "app.read": {"title": "Read app", "category": "business"}
    }
    app["definition"]["services"]["app.service"]["methods"]["read"].update(
        audience="business", required_scopes=["app.read"]
    )
    graph = Graph(
        {
            "app": app,
            "mcp": candidate("mcp", provider="mcp"),
            "docs": candidate("docs", provider="llms"),
        },
        {},
    )
    report = snapshot(graph)
    assert report["impacts"]["app"]["reload"]["affected"] == ["app"]
    assert report["impacts"]["app"]["disable"]["affected"] == ["app", "mcp"]
    assert {
        item["provider"] for item in report["impacts"]["app"]["reload"]["presentations"]
    } == {"mcp", "docs"}
