"""Prepare native sources and a separate wheelhouse used only for CI acceptance."""

import argparse
import json
import os
import subprocess
import sys

from validate import CORE, prepare_siblings, snapshot_core


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--repository-owner", required=True)
    args = parser.parse_args()
    root = CORE / "build/launcher-inputs"
    core = root / "workspace/toolframe-core"
    core.mkdir(parents=True, exist_ok=False)
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    pins = json.loads((CORE / "scripts/ci/dependencies.json").read_text())
    snapshot_core(core)
    prepare_siblings(root, pins, CORE.parent, args.repository_owner, env)
    wheels = root / "validation-wheels"
    wheels.mkdir()
    for repository in ("toolframe-core", *pins):
        subprocess.run(
            [
                "uv",
                "build",
                "--no-sources",
                "--wheel",
                "--out-dir",
                str(wheels),
                str(root / "workspace" / repository),
            ],
            env=env,
            check=True,
        )
    if args.target == "x86_64-apple-darwin":
        subprocess.run(
            [
                sys.executable,
                str(root / "workspace/toolframe-fastmcp/scripts/build-macos-intel.py"),
                "--out-dir",
                str(wheels),
            ],
            env=env,
            check=True,
        )
    subprocess.run(
        [
            sys.executable,
            str(CORE / "scripts/build-launcher.py"),
            "prepare",
            "--target",
            args.target,
            "--output",
            str(CORE / "build/launcher-dist"),
            "--native-wheelhouse",
            str(wheels),
        ],
        env=env,
        check=True,
    )


if __name__ == "__main__":
    main()
