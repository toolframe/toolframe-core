#!/usr/bin/env python3
"""Sync the standalone Python package into Toolframe's embedded runtime."""

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import tomllib
from pathlib import Path


def main() -> None:
    repository = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, default=repository.parent / "fastapi-cordis-reactive"
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    metadata = tomllib.loads((source / "pyproject.toml").read_text())
    package = source / "src" / "fastapi_cordis"
    expected = {
        "__init__.py",
        "_errors.py",
        "_events.py",
        "_graph.py",
        "_runtime.py",
        "_scope.py",
        "_services.py",
        "fastapi.py",
        "py.typed",
    }
    actual = {file.name for file in package.iterdir() if file.is_file()}
    if actual != expected:
        raise SystemExit(
            "Package file list changed; update the Rust embedded file list before syncing"
        )
    files = {name: (package / name).read_bytes() for name in sorted(expected)}
    files["LICENSE"] = (source / "LICENSE").read_bytes()
    wheel_name = f"fastapi_cordis-{metadata['project']['version']}-py3-none-any.whl"
    if wheel_name != "fastapi_cordis-0.2.0-py3-none-any.whl":
        raise SystemExit(
            "Update Toolframe's pinned dependency and embedded wheel filename before syncing a new version"
        )
    with tempfile.TemporaryDirectory(prefix="toolframe-plugin-wheel-") as temporary:
        subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", temporary, str(source)],
            check=True,
            env={**os.environ, "SOURCE_DATE_EPOCH": "1704067200"},
        )
        wheel = (Path(temporary) / wheel_name).read_bytes()
    manifest = {
        "name": metadata["project"]["name"],
        "version": metadata["project"]["version"],
        "source": f"{source.name}/src/fastapi_cordis",
        "requiresPython": metadata["project"]["requires-python"],
        "dependencies": metadata["project"]["dependencies"],
        "wheel": {"filename": wheel_name, "sha256": hashlib.sha256(wheel).hexdigest()},
        "sha256": {
            name: hashlib.sha256(data).hexdigest() for name, data in files.items()
        },
    }
    files["UPSTREAM.json"] = (
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    ).encode()
    destination = repository / "core" / "fastapi_cordis"
    mismatches = [
        name
        for name, data in files.items()
        if not (destination / name).is_file()
        or (destination / name).read_bytes() != data
    ]
    wheel_destination = repository / "core" / wheel_name
    if not wheel_destination.is_file() or wheel_destination.read_bytes() != wheel:
        mismatches.append(wheel_name)
    if args.check:
        if mismatches:
            raise SystemExit(
                "Embedded fastapi-cordis is stale: " + ", ".join(mismatches)
            )
        print(f"fastapi-cordis {manifest['version']}: embedded source matches")
        return
    destination.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (destination / name).write_bytes(data)
    wheel_destination.write_bytes(wheel)
    print(f"Synced fastapi-cordis {manifest['version']} ({len(files)} files)")


if __name__ == "__main__":
    main()
