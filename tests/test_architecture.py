"""Keep independently installable repositories behind public contracts."""

import ast
import tomllib

import pytest
from dependencies import source_tree
from packaging.requirements import Requirement

REPOSITORIES = (
    "toolframe-core",
    "toolframe-web",
    "toolframe-sdk",
    "toolframe-sql-sqlite",
    "toolframe-sql-postgres",
    "toolframe-streamlit-dashboard",
    "toolframe-fastmcp",
    "toolframe-llms-txt",
    "toolframe-demo-entry-register",
    "toolframe-auth",
    "toolframe-dashboard-auth",
    "toolframe-mcp-auth",
    "toolframe-oidc-auth",
)


@pytest.mark.parametrize(
    "repository",
    (*REPOSITORIES, "toolframe-sample-notes", "toolframe-web-notes", "toolframe-basic"),
)
def test_repository_uses_only_its_implementation_and_public_sdk(repository):
    templates = {
        "toolframe-sample-notes": (
            "toolframe-streamlit-dashboard",
            "src/toolframe_streamlit_dashboard/templates/notes",
        ),
        "toolframe-web-notes": ("toolframe-web", "src/toolframe_web/templates/notes"),
        "toolframe-basic": ("toolframe-core", "src/toolframe_core/basic_template"),
    }
    owner, relative = templates.get(repository, (repository, ""))
    root = source_tree(owner) / relative
    assert root.is_dir(), f"Integration workspace is missing {repository}"
    own = repository.replace("-", "_")
    allowed = {own, "toolframe_sdk"}
    if repository == "toolframe-web-notes":
        allowed.add(
            "toolframe_web"
        )  # Public presentation helpers live in the web extra.
    forbidden_libraries = (
        {"streamlit", "fastmcp"}
        if repository in {"toolframe-core", "toolframe-sdk"}
        else set()
    )
    if repository in {
        "toolframe-demo-entry-register",
        "toolframe-sample-notes",
        "toolframe-auth",
        "toolframe-dashboard-auth",
        "toolframe-mcp-auth",
        "toolframe-oidc-auth",
    }:
        forbidden_libraries.add("sqlite3")
    for file in (root / "src").rglob("*.py"):
        # Shipped authoring assets are a separate plugin, checked in their own case.
        if repository in REPOSITORIES and {
            "bundled_skills",
            "basic_template",
            "templates",
        } & set(file.parts):
            continue
        for node in ast.walk(ast.parse(file.read_text(), filename=str(file))):
            imports = []
            if isinstance(node, ast.Import):
                imports = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                imports = [node.module or ""]
            for imported in imports:
                top = imported.split(".")[0]
                assert not (top.startswith("toolframe_") and top not in allowed), (
                    f"{file}:{node.lineno} imports another repository's implementation: {imported}"
                )
                assert top not in forbidden_libraries, (
                    f"{file}:{node.lineno} imports an implementation outside its role: {imported}"
                )
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    dependencies = project.get("dependencies", []) + [
        dependency
        for group in project.get("optional-dependencies", {}).values()
        for dependency in group
    ]
    for dependency in dependencies:
        name = Requirement(dependency).name.replace("-", "_")
        assert not (name.startswith("toolframe_") and name not in allowed)
        assert name not in forbidden_libraries
