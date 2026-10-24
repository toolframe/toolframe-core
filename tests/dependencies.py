"""Use explicit feature wheels and published dependencies in isolated package checks.

Release CI can still supply its pinned sibling source checkouts. TFDEV_OUTPUT or
TOOLFRAME_TEST_WHEELHOUSE selects the wheel-based package verification boundary.
"""

from __future__ import annotations

import configparser
import email
import hashlib
import json
import os
import urllib.request
import zipfile
from pathlib import Path

import tomlkit

ROOT = Path(__file__).resolve().parents[2]
BUILD = Path(__file__).resolve().parents[1] / "build/package-fixtures"
VERSIONS = {
    "toolframe-sdk": "0.4.0+web.1",
    "toolframe-core": "0.8.0",
    "toolframe-web": "0.1.0",
    "toolframe-streamlit-dashboard": "0.5.1",
    "toolframe-dashboard-auth": "0.2.1",
    "toolframe-sql-sqlite": "0.4.0",
    "toolframe-sql-postgres": "0.2.0",
    "toolframe-fastmcp": "0.3.0",
    "toolframe-llms-txt": "0.3.0",
    "toolframe-demo-entry-register": "0.4.0",
    "toolframe-auth": "0.3.0",
    "toolframe-mcp-auth": "0.3.0",
    "toolframe-oidc-auth": "0.3.0",
}


def isolated():
    return bool(
        os.environ.get("TFDEV_OUTPUT") or os.environ.get("TOOLFRAME_TEST_WHEELHOUSE")
    )


def wheel(name):
    output = os.environ.get("TFDEV_OUTPUT")
    override = os.environ.get("TOOLFRAME_TEST_WHEELHOUSE")
    choices = list((Path(output) / "wheels" / name).glob("*.whl")) if output else []
    if not choices and override:
        choices = list(
            Path(override).glob(
                name.replace("-", "_") + "-" + VERSIONS[name] + "-*.whl"
            )
        )
    if choices:
        assert len(choices) == 1, f"Ambiguous selected wheel for {name}"
        return choices[0]
    assert name not in {
        "toolframe-sdk",
        "toolframe-core",
        "toolframe-dashboard-auth",
        "toolframe-streamlit-dashboard",
        "toolframe-web",
    }, f"Supply the changed {name} wheel explicitly"
    directory = BUILD / "downloads"
    directory.mkdir(parents=True, exist_ok=True)
    candidates = list(
        directory.glob(name.replace("-", "_") + "-" + VERSIONS[name] + "-*.whl")
    )
    receipt = directory / (name + ".json")
    if candidates and receipt.exists():
        value = json.loads(receipt.read_text())
        assert hashlib.sha256(candidates[0].read_bytes()).hexdigest() == value["sha256"]
        return candidates[0]
    with urllib.request.urlopen(
        f"https://pypi.org/pypi/{name}/{VERSIONS[name]}/json", timeout=30
    ) as response:
        release = json.load(response)
    artifact = next(
        item
        for item in release["urls"]
        if item["filename"].endswith("-py3-none-any.whl")
    )
    target = directory / artifact["filename"]
    with urllib.request.urlopen(artifact["url"], timeout=30) as response:
        data = response.read(32 * 1024 * 1024 + 1)
    assert len(data) <= 32 * 1024 * 1024
    assert hashlib.sha256(data).hexdigest() == artifact["digests"]["sha256"]
    target.write_bytes(data)
    receipt.write_text(
        json.dumps({"source": artifact["url"], "sha256": artifact["digests"]["sha256"]})
    )
    return target


def wheelhouse():
    return wheel("toolframe-sdk").parent if isolated() else ROOT / "toolframe-sdk/dist"


def source(name):
    return str(wheel(name)) if isolated() else str(ROOT / name)


def source_tree(name):
    if not isolated():
        return ROOT / name
    selected = json.loads(os.environ.get("TFDEV_REPOSITORIES", "{}"))
    if name in selected:
        return Path(selected[name])
    if name == "toolframe-core":
        return Path(__file__).resolve().parents[1]
    artifact = wheel(name)
    identity = hashlib.sha256(artifact.read_bytes()).hexdigest()
    target = BUILD / "sources" / name / identity
    if (target / "pyproject.toml").exists():
        return target
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(artifact) as archive:
        metadata_path = next(
            path for path in archive.namelist() if path.endswith(".dist-info/METADATA")
        )
        metadata = email.message_from_bytes(archive.read(metadata_path))
        entrypoints = configparser.ConfigParser()
        entries = metadata_path.rsplit("/", 1)[0] + "/entry_points.txt"
        if entries in archive.namelist():
            entrypoints.read_string(archive.read(entries).decode())
        for item in archive.namelist():
            parts = Path(item).parts
            if (
                not parts
                or any(part in {"..", "/"} for part in parts)
                or ".dist-info" in parts[0]
                or item.endswith("/")
            ):
                continue
            path = target / "src" / item
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.read(item))
    requirements = metadata.get_all("Requires-Dist", [])
    extras = metadata.get_all("Provides-Extra", [])
    project = {
        "name": metadata["Name"],
        "version": metadata["Version"],
        "requires-python": metadata.get("Requires-Python", ">=3.14"),
        "dependencies": [item for item in requirements if "extra ==" not in item],
        "optional-dependencies": {
            extra: [
                item
                for item in requirements
                if f"extra == '{extra}'" in item or f'extra == "{extra}"' in item
            ]
            for extra in extras
        },
        "entry-points": {
            section: dict(entrypoints[section]) for section in entrypoints.sections()
        },
    }
    packages = [
        "src/" + path.name for path in (target / "src").iterdir() if path.is_dir()
    ]
    (target / "pyproject.toml").write_text(
        tomlkit.dumps(
            {
                "build-system": {
                    "requires": ["hatchling>=1.27,<2"],
                    "build-backend": "hatchling.build",
                },
                "project": project,
                "tool": {
                    "hatch": {"build": {"targets": {"wheel": {"packages": packages}}}}
                },
            }
        )
    )
    return target
