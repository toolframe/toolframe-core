"""Keep independently installable repositories behind public contracts."""

import ast
import tomllib
from importlib.util import resolve_name

import pytest
from dependencies import source_tree
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

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
    check_repository(root, repository)


def check_repository(root, repository):
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
        operator = (
            repository == "toolframe-oidc-auth"
            and file.relative_to(root / "src").as_posix()
            == "toolframe_oidc_auth/maintenance.py"
        )
        for node in ast.walk(ast.parse(file.read_text(), filename=str(file))):
            # Only this explicit public symbol is available to the offline CLI.
            if (
                operator
                and isinstance(node, ast.ImportFrom)
                and node.level == 0
                and node.module == "toolframe_core.maintenance"
                and all(item.name == "stopped_application" for item in node.names)
            ):
                continue
            if repository == "toolframe-oidc-auth" and not operator:
                imported = []
                if isinstance(node, ast.Import):
                    imported = [item.name for item in node.names]
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if node.level:
                        package = ".".join(file.relative_to(root / "src").parts[:-1])
                        module = resolve_name("." * node.level + module, package)
                    imported = [
                        module,
                        *(module + "." + item.name for item in node.names),
                    ]
                assert not any(
                    name == "toolframe_oidc_auth.maintenance"
                    or name.startswith("toolframe_oidc_auth.maintenance.")
                    for name in imported
                ), f"{file}:{node.lineno} imports the operator-only maintenance module"
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
                assert top not in forbidden_libraries or (
                    operator and top == "sqlite3"
                ), (
                    f"{file}:{node.lineno} imports an implementation outside its role: {imported}"
                )
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    groups = {
        None: project.get("dependencies", []),
        **project.get("optional-dependencies", {}),
    }
    for extra, dependencies in groups.items():
        for dependency in dependencies:
            name = canonicalize_name(Requirement(dependency).name).replace("-", "_")
            if (
                repository == "toolframe-oidc-auth"
                and extra == "operator"
                and name == "toolframe_core"
            ):
                continue
            assert not (name.startswith("toolframe_") and name not in allowed), (
                f"{repository} has a forbidden dependency in {extra or 'runtime'}: {name}"
            )
            assert name not in forbidden_libraries


@pytest.fixture
def oidc_repository(tmp_path):
    package = tmp_path / "src/toolframe_oidc_auth"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "plugin.py").write_text("from toolframe_sdk import Plugin\n")
    (package / "maintenance.py").write_text(
        "import sqlite3\nfrom toolframe_core.maintenance import stopped_application\n"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="toolframe-oidc-auth"\ndependencies=["toolframe-sdk>=0.4,<0.5"]\n'
        '[project.optional-dependencies]\noperator=["toolframe-core>=0.8.0,<1"]\n'
    )
    return tmp_path


def test_oidc_operator_public_boundary_is_separate_from_runtime(oidc_repository):
    check_repository(oidc_repository, "toolframe-oidc-auth")


@pytest.mark.parametrize(
    "module,source,reason",
    [
        ("plugin.py", "import sqlite3", "outside its role"),
        ("plugin.py", "from sqlite3 import connect", "outside its role"),
        (
            "plugin.py",
            "from toolframe_core.maintenance import stopped_application",
            "another repository",
        ),
        (
            "maintenance.py",
            "from toolframe_core.maintenance import application_lock",
            "another repository",
        ),
        (
            "maintenance.py",
            "from toolframe_core.maintenance import _read_layout",
            "another repository",
        ),
        (
            "maintenance.py",
            "from toolframe_core.maintenance import stopped_application, HostLock",
            "another repository",
        ),
        (
            "maintenance.py",
            "from toolframe_core.maintenance import *",
            "another repository",
        ),
        ("maintenance.py", "import toolframe_core.maintenance", "another repository"),
        (
            "maintenance.py",
            "from toolframe_core import maintenance",
            "another repository",
        ),
        (
            "maintenance.py",
            "from toolframe_core.state import SQLiteCoreState",
            "another repository",
        ),
        ("maintenance.py", "import toolframe_auth.plugin", "another repository"),
        ("cli.py", "import sqlite3", "outside its role"),
        ("nested/maintenance.py", "import sqlite3", "outside its role"),
        (
            "plugin.py",
            "import toolframe_oidc_auth.maintenance as recovery",
            "operator-only",
        ),
        (
            "plugin.py",
            "from toolframe_oidc_auth.maintenance import main",
            "operator-only",
        ),
        (
            "plugin.py",
            "from toolframe_oidc_auth import maintenance as recovery",
            "operator-only",
        ),
        ("plugin.py", "from .maintenance import main", "operator-only"),
        ("plugin.py", "from . import maintenance", "operator-only"),
        ("__init__.py", "from .maintenance import recover_legacy", "operator-only"),
        ("nested/worker.py", "from .. import maintenance", "operator-only"),
    ],
)
def test_oidc_operator_exception_rejects_import_boundary_violations(
    oidc_repository, module, source, reason
):
    file = oidc_repository / "src/toolframe_oidc_auth" / module
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(source + "\n")
    with pytest.raises(AssertionError, match=reason):
        check_repository(oidc_repository, "toolframe-oidc-auth")


@pytest.mark.parametrize(
    "extra,dependency",
    [
        (None, "toolframe-core>=0.8"),
        (None, "Toolframe.Core>=0.8"),
        ("web", "toolframe-core>=0.8"),
        ("maintenance", "toolframe-core>=0.8"),
        ("operator", "toolframe-auth>=0.3"),
        ("operator", "toolframe-sql-sqlite>=0.4"),
        ("operator", "sqlite3"),
    ],
)
def test_oidc_core_dependency_is_only_for_operator_extra(
    oidc_repository, extra, dependency
):
    project = oidc_repository / "pyproject.toml"
    if extra is None:
        project.write_text(f'[project]\ndependencies=["{dependency}"]\n')
    else:
        project.write_text(
            f'[project]\ndependencies=[]\n[project.optional-dependencies]\n{extra}=["{dependency}"]\n'
        )
    with pytest.raises(AssertionError):
        check_repository(oidc_repository, "toolframe-oidc-auth")
