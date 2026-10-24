"""Run CI in a new workspace; never build or sync the source sibling checkouts."""

import argparse
import base64
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlparse

CORE = Path(__file__).resolve().parents[2]
PYTHON = "3.14.7"


def run(*command, cwd, env):
    command = list(map(str, command))
    print(f"+ ({cwd}) {shlex.join(command)}", flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def capture(*command, cwd):
    return subprocess.check_output(command, cwd=cwd, text=True).strip()


def snapshot_core(destination):
    # Include current tracked edits and new CI files, but no ignored build/cache files.
    names = (
        subprocess.check_output(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=CORE,
        )
        .decode()
        .split("\0")
    )
    for name in filter(None, names):
        source = CORE / name
        if source.is_file():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def prepare_sdist(root, repository, pin):
    if set(pin) != {"version", "sha256"} or not re.fullmatch(
        r"[0-9a-f]{64}", pin["sha256"]
    ):
        raise ValueError(f"Invalid distribution pin: {repository}")
    version = pin["version"]
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}", version):
        raise ValueError(f"Invalid distribution version: {repository}")
    name = f"{repository.replace('-', '_')}-{version}"
    filename = name + ".tar.gz"
    supplied = os.environ.get("TOOLFRAME_CI_SDIST_DIR")
    if supplied:
        data = (Path(supplied) / filename).read_bytes()
    else:
        with urllib.request.urlopen(
            f"https://pypi.org/pypi/{repository}/{version}/json", timeout=30
        ) as response:
            release = json.load(response)
        artifact = next(
            item
            for item in release["urls"]
            if item["filename"] == filename and item["packagetype"] == "sdist"
        )
        url = urlparse(artifact["url"])
        if url.scheme != "https" or url.hostname != "files.pythonhosted.org":
            raise ValueError(f"Unexpected distribution host: {repository}")
        if artifact["digests"]["sha256"] != pin["sha256"]:
            raise ValueError(f"Distribution metadata hash mismatch: {repository}")
        with urllib.request.urlopen(artifact["url"], timeout=30) as response:
            data = response.read(32 * 1024 * 1024 + 1)
    if (
        len(data) > 32 * 1024 * 1024
        or hashlib.sha256(data).hexdigest() != pin["sha256"]
    ):
        raise ValueError(f"Distribution hash mismatch: {repository}")
    archive = root / filename
    archive.write_bytes(data)
    staging = root / "sdists" / repository
    staging.mkdir(parents=True)
    with tarfile.open(archive) as bundle:
        if any(Path(member.name).parts[0] != name for member in bundle.getmembers()):
            raise ValueError(f"Unexpected distribution layout: {repository}")
        bundle.extractall(staging, filter="data")
    source = staging / name
    project = tomllib.loads((source / "pyproject.toml").read_text())["project"]
    if project["name"] != repository or project["version"] != version:
        raise ValueError(f"Distribution identity mismatch: {repository}")
    source.rename(root / "workspace" / repository)


def prepare_siblings(root, pins, source_root, owner, env):
    for repository, commit in pins.items():
        if not re.fullmatch(r"[a-z0-9-]+", repository):
            raise ValueError(f"Invalid dependency name: {repository}")
        if isinstance(commit, dict):
            prepare_sdist(root, repository, commit)
            continue
        if not re.fullmatch(r"[a-z0-9-]+", repository) or not re.fullmatch(
            r"[0-9a-f]{40}", commit
        ):
            raise ValueError(f"Invalid dependency pin: {repository} {commit}")
        source = source_root / repository
        if owner:
            source = root / "sources" / repository
            source.mkdir(parents=True)
            run("git", "init", "--quiet", cwd=source, env=env)
            git_env = dict(env)
            if token := os.environ.get("TOOLFRAME_REPOSITORY_TOKEN"):
                credentials = base64.b64encode(
                    f"x-access-token:{token}".encode()
                ).decode()
                git_env.update(
                    GIT_CONFIG_COUNT="1",
                    GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                    GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {credentials}",
                )
            run(
                "git",
                "fetch",
                "--depth=1",
                "--no-tags",
                f"https://github.com/{owner}/{repository}.git",
                commit,
                cwd=source,
                env=git_env,
            )
        actual = capture("git", "rev-parse", f"{commit}^{{commit}}", cwd=source)
        if actual != commit:
            raise RuntimeError(f"Dependency pin mismatch: {repository}: {actual}")
        archive = root / f"{repository}.tar"
        run("git", "archive", "--output", archive, commit, cwd=source, env=env)
        destination = root / "workspace" / repository
        destination.mkdir()
        with tarfile.open(archive) as bundle:
            bundle.extractall(destination, filter="data")


def verify_licenses(core, distribution):
    project = tomllib.loads((core / "pyproject.toml").read_text())["project"]
    wheel = next(distribution.glob("*.whl"))
    with zipfile.ZipFile(wheel) as bundle:
        for name in project["license-files"]:
            member = f"toolframe_core-{project['version']}.dist-info/licenses/{name}"
            assert bundle.read(member) == (core / name).read_bytes(), member
        assert not any("/evals/" in name for name in bundle.namelist())
    with tarfile.open(next(distribution.glob("*.tar.gz"))) as bundle:
        for name in project["license-files"]:
            member = f"toolframe_core-{project['version']}/{name}"
            assert bundle.extractfile(member).read() == (core / name).read_bytes(), (
                member
            )


def python_checks(root, core, output, env):
    sdk_dist = root / "workspace/toolframe-sdk/dist"
    dependencies = root / "dependency-wheels"
    run(
        "uv",
        "build",
        "--no-sources",
        "--wheel",
        "--out-dir",
        sdk_dist,
        root / "workspace/toolframe-sdk",
        cwd=core,
        env=env,
    )
    run(
        "uv",
        "build",
        "--no-sources",
        "--wheel",
        "--out-dir",
        dependencies,
        root / "workspace/fastapi-cordis",
        cwd=core,
        env=env,
    )
    run(
        "uv",
        "sync",
        "--locked",
        "--no-editable",
        "--group",
        "dev",
        "--python",
        PYTHON,
        cwd=core,
        env=env,
    )
    run(
        "uv",
        "run",
        "--no-sync",
        "ruff",
        "check",
        "src",
        "tests",
        "scripts/build-launcher.py",
        "scripts/verify-release.py",
        "scripts/verify-skill.py",
        "scripts/verify-scaffold.py",
        "scripts/check-authoring-catalog.py",
        "scripts/ci",
        cwd=core,
        env=env,
    )
    run(
        "uv",
        "run",
        "--no-sync",
        "pytest",
        "-q",
        f"--junitxml={output / 'pytest.xml'}",
        cwd=core,
        env=env,
    )
    distribution = output / "python-dist"
    run("uv", "build", "--no-sources", "--out-dir", distribution, cwd=core, env=env)
    authoring_wheels = root / "authoring-wheels"
    authoring_wheels.mkdir()
    for wheel in distribution.glob("*.whl"):
        shutil.copy2(wheel, authoring_wheels / wheel.name)
    for name in ("toolframe-web", "toolframe-streamlit-dashboard"):
        run(
            "uv",
            "build",
            "--no-sources",
            "--wheel",
            "--out-dir",
            authoring_wheels,
            root / "workspace" / name,
            cwd=core,
            env=env,
        )
    run(
        "uv",
        "run",
        "--no-sync",
        "python",
        "scripts/check-authoring-catalog.py",
        "--wheelhouse",
        authoring_wheels,
        cwd=core,
        env=env,
    )
    run(
        "uv",
        "tool",
        "run",
        "--python",
        PYTHON,
        "--from",
        "twine==7.0.0",
        "twine",
        "check",
        "--strict",
        *sorted(distribution.glob("*.whl")),
        *sorted(distribution.glob("*.tar.gz")),
        cwd=core,
        env=env,
    )
    verify_licenses(core, distribution)
    installed = root / "installed-wheel"
    run("uv", "venv", "--python", PYTHON, installed, cwd=root, env=env)
    python = installed / "bin/python"
    run(
        "uv",
        "pip",
        "install",
        "--python",
        python,
        *sorted(distribution.glob("*.whl")),
        *sorted(sdk_dist.glob("*.whl")),
        *sorted(dependencies.glob("*.whl")),
        cwd=root,
        env=env,
    )
    run("uv", "pip", "check", "--python", python, cwd=root, env=env)
    version = tomllib.loads((core / "pyproject.toml").read_text())["project"]["version"]
    smoke = """
import importlib.metadata as metadata
import importlib.util
import json
import sys
from pathlib import Path
import fastapi_cordis
import toolframe_core
import toolframe_sdk
from toolframe_core.api import create_app
from toolframe_core.host import Host
assert metadata.version('toolframe-core') == sys.argv[1]
assert Path(toolframe_core.__file__).is_relative_to(Path(sys.prefix))
assert importlib.util.find_spec('opentelemetry') is None
print(json.dumps({'version': metadata.version('toolframe-core'),
                  'installed_module': toolframe_core.__file__,
                  'python': sys.version, 'runtime_imports': 'passed'}))
"""
    run(python, "-I", "-c", smoke, version, cwd=root, env=env)
    run(python, "-I", "-m", "toolframe_core.cli", "--help", cwd=root, env=env)


def launcher_checks(root, core, output, target, env):
    run("cargo", "fmt", "--check", cwd=core, env=env)
    run("cargo", "test", "--locked", cwd=core, env=env)
    wheels = root / "validation-wheels"
    wheels.mkdir()
    for repository in sorted((root / "workspace").iterdir()):
        run(
            "uv",
            "build",
            "--no-sources",
            "--wheel",
            "--out-dir",
            wheels,
            repository,
            cwd=core,
            env=env,
        )
    if target == "x86_64-apple-darwin":
        run("rustup", "target", "add", target, cwd=core, env=env)
        run(
            sys.executable,
            "scripts/ci/intel_companion.py",
            "fetch",
            "--out-dir",
            wheels,
            cwd=core,
            env=env,
        )
    for action in ("prepare", "build", "finish"):
        run(
            sys.executable,
            "scripts/build-launcher.py",
            action,
            "--target",
            target,
            "--output",
            output / "launcher",
            "--native-wheelhouse",
            wheels,
            cwd=core,
            env=env,
        )
    architecture = target.split("-", 1)[0]
    operating_system = "macos" if "apple-darwin" in target else "linux"
    libc = "none" if operating_system == "macos" else "gnu"
    python = f"cpython-{PYTHON}-{operating_system}-{architecture}-{libc}"
    if operating_system == "macos" and architecture == "aarch64":
        architecture = "arm64"
    elif operating_system == "macos" and platform.machine() == "arm64":
        architecture += "-rosetta"
    run(
        sys.executable,
        "scripts/ci/check-launcher-wheel.py",
        "--output",
        output / "launcher",
        "--wheelhouse",
        wheels,
        "--python",
        python,
        "--architecture",
        architecture,
        cwd=core,
        env=env,
    )


def postgres_checks(root, core, output, env):
    if not os.environ.get("TOOLFRAME_CORE_TEST_ADMIN_DSN"):
        raise RuntimeError(
            "PostgreSQL validation requires TOOLFRAME_CORE_TEST_ADMIN_DSN"
        )
    run(
        "uv",
        "build",
        "--no-sources",
        "--wheel",
        "--out-dir",
        root / "workspace/toolframe-sdk/dist",
        root / "workspace/toolframe-sdk",
        cwd=core,
        env=env,
    )
    run(
        "uv",
        "sync",
        "--locked",
        "--group",
        "dev",
        "--extra",
        "postgres",
        "--python",
        PYTHON,
        cwd=core,
        env=env,
    )
    run(
        "uv",
        "pip",
        "install",
        "--python",
        core / ".venv/bin/python",
        "--no-sources",
        "--find-links",
        root / "workspace/toolframe-sdk/dist",
        root / "workspace/toolframe-sql-postgres",
        cwd=core,
        env=env,
    )
    env = {
        **env,
        "TOOLFRAME_CORE_TEST_ADMIN_DSN": os.environ["TOOLFRAME_CORE_TEST_ADMIN_DSN"],
        **{
            key: os.environ[key]
            for key in ("TOOLFRAME_POSTGRES_PG_DUMP", "TOOLFRAME_POSTGRES_PG_RESTORE")
            if key in os.environ
        },
    }
    run(
        "uv",
        "run",
        "--no-sync",
        "python",
        "scripts/ci/check-postgres.py",
        cwd=core,
        env=env,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--checks", choices=("python", "launcher", "postgres"), required=True
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="A new evidence directory"
    )
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument("--source-root", type=Path, default=CORE.parent)
    sources.add_argument(
        "--repository-owner", help="Fetch public pinned siblings from GitHub"
    )
    parser.add_argument(
        "--target",
        choices=(
            "aarch64-apple-darwin",
            "x86_64-apple-darwin",
            "aarch64-unknown-linux-gnu",
            "x86_64-unknown-linux-gnu",
        ),
    )
    args = parser.parse_args()
    if args.repository_owner and not re.fullmatch(
        r"[A-Za-z0-9-]+", args.repository_owner
    ):
        parser.error("repository owner must be a GitHub user or organization name")
    if sys.version_info[:3] != (3, 14, 7):
        parser.error("Run this script with Python 3.14.7")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    root = Path(tempfile.mkdtemp(prefix="toolframe-core-ci-"))
    core = root / "workspace/toolframe-core"
    core.mkdir(parents=True)
    (output / "workspace.txt").write_text(f"{root}\n")
    print(f"Fresh workspace: {root}; evidence: {output}", flush=True)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("TOOLFRAME_", "UV_"))
        and key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CARGO_BUILD_TARGET"}
    }
    env.update(
        UV_CACHE_DIR=str(root / "uv-cache"),
        UV_PYTHON_INSTALL_DIR=str(root / "python"),
        UV_TOOL_DIR=str(root / "uv-tools"),
        CARGO_TARGET_DIR=str(root / "cargo-target"),
        CARGO_BUILD_JOBS="2",
    )
    uv = capture("uv", "--version", cwd=CORE)
    if not uv.startswith("uv 0.12.8 "):
        parser.error(f"Use uv 0.12.8 for CI parity; found {uv}")
    pins = json.loads((CORE / "scripts/ci/dependencies.json").read_text())
    evidence = {
        "core_commit": capture("git", "rev-parse", "HEAD", cwd=CORE),
        "core_dirty": bool(capture("git", "status", "--porcelain", cwd=CORE)),
        "dependencies": pins,
        "workspace": str(root),
        "uv": uv,
        "python": sys.version,
        "platform": platform.platform(),
        "checks": args.checks,
        "passed": False,
    }
    evidence_file = output / "validation.json"
    evidence_file.write_text(json.dumps(evidence, indent=2) + "\n")
    snapshot_core(core)
    prepare_siblings(root, pins, args.source_root, args.repository_owner, env)
    if args.checks == "python":
        python_checks(root, core, output, env)
    elif args.checks == "postgres":
        postgres_checks(root, core, output, env)
    else:
        target = args.target or (
            ("aarch64" if platform.machine() in ("arm64", "aarch64") else "x86_64")
            + ("-apple-darwin" if sys.platform == "darwin" else "-unknown-linux-gnu")
        )
        evidence["target"] = target
        launcher_checks(root, core, output, target, env)
    evidence["passed"] = True
    evidence["artifacts"] = {
        str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
        for folder in ("python-dist", "launcher")
        for path in (output / folder).rglob("*")
        if path.is_file()
    }
    evidence_file.write_text(json.dumps(evidence, indent=2) + "\n")
    print(f"Validation passed: {evidence_file}", flush=True)


if __name__ == "__main__":
    main()
