"""Run CI in a new workspace; never build or sync the source sibling checkouts."""

import argparse
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
import zipfile
from pathlib import Path

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


def prepare_siblings(root, pins, source_root, owner, env):
    for repository, commit in pins.items():
        if not re.fullmatch(r"[a-z0-9-]+", repository) or not re.fullmatch(
            r"[0-9a-f]{40}", commit
        ):
            raise ValueError(f"Invalid dependency pin: {repository} {commit}")
        source = source_root / repository
        if owner:
            source = root / "sources" / repository
            source.mkdir(parents=True)
            run("git", "init", "--quiet", cwd=source, env=env)
            run(
                "git",
                "fetch",
                "--depth=1",
                "--no-tags",
                f"https://github.com/{owner}/{repository}.git",
                commit,
                cwd=source,
                env=env,
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
        "scripts/verify-release.py",
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
    if sys.platform != "darwin":
        raise RuntimeError("Launcher validation requires macOS")
    rust = capture("rustc", "--version", cwd=core)
    if not rust.startswith("rustc 1.94.1 "):
        raise RuntimeError(f"Use Rust 1.94.1 for CI parity; found {rust}")
    run("cargo", "fmt", "--check", cwd=core, env=env)
    run("cargo", "check", "--locked", "--all-targets", cwd=core, env=env)
    run("cargo", "test", "--locked", cwd=core, env=env)
    native = root / "native-wheels"
    native.mkdir()
    if target == "x86_64-apple-darwin":
        run("rustup", "target", "add", target, cwd=core, env=env)
        # This existing script verifies pinned source hashes and statically builds OpenSSL.
        run(
            sys.executable,
            root / "workspace/toolframe-fastmcp/scripts/build-macos-intel.py",
            "--out-dir",
            native,
            cwd=core,
            env=env,
        )
    run(
        "bash",
        "scripts/build-release.sh",
        "--target",
        target,
        "--native-wheelhouse",
        native,
        "--locked",
        cwd=core,
        env=env,
    )
    version = tomllib.loads((core / "Cargo.toml").read_text())["package"]["version"]
    binary = core / f"dist/toolframe-{version}-standard-{target}"
    architecture = "arm64" if target.startswith("aarch64") else "x86_64"
    if architecture == "x86_64" and platform.machine() == "arm64":
        architecture += "-rosetta"
    run("file", binary, cwd=core, env=env)
    run(
        sys.executable,
        "scripts/verify-release.py",
        "--binary",
        binary,
        "--architecture",
        architecture,
        "--output",
        output / "release.json",
        cwd=core,
        env=env,
    )
    run(
        sys.executable,
        "scripts/verify-skill.py",
        "--binary",
        binary,
        "--output",
        output / "skill.json",
        cwd=core,
        env=env,
    )
    artifacts = output / "launcher"
    artifacts.mkdir()
    archive = artifacts / f"{binary.name}.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(binary, arcname=binary.name)
        for name in ("LICENSE", "NOTICE"):
            bundle.add(core / name, arcname=name)
        for name, destination in (
            ("LICENSE", "licenses/plugin-design-MIT.txt"),
            ("assets/sample-plugin/LICENSE", "licenses/sample-plugin-MIT.txt"),
        ):
            bundle.add(
                core
                / "src/toolframe_core/bundled_skills/toolframe-plugin-design"
                / name,
                arcname=destination,
            )
    (artifacts / "SHA256SUMS").write_text(
        f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n"
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
    env = {
        **env,
        "TOOLFRAME_CORE_TEST_ADMIN_DSN": os.environ["TOOLFRAME_CORE_TEST_ADMIN_DSN"],
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
        "--target", choices=("aarch64-apple-darwin", "x86_64-apple-darwin")
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
            "aarch64-apple-darwin"
            if platform.machine() == "arm64"
            else "x86_64-apple-darwin"
        )
        evidence["target"] = target
        launcher_checks(root, core, output, target, env)
    evidence["passed"] = True
    evidence["artifacts"] = {
        str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
        for folder in ("python-dist", "launcher")
        for path in (output / folder).glob("*")
        if path.is_file()
    }
    evidence_file.write_text(json.dumps(evidence, indent=2) + "\n")
    print(f"Validation passed: {evidence_file}", flush=True)


if __name__ == "__main__":
    main()
