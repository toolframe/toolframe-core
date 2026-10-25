"""Install the native wheel as a uv tool and exercise its public CLI."""

import argparse
import hashlib
import os
import platform
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--python", default="3.14.7")
    parser.add_argument("--architecture", default=platform.machine())
    args = parser.parse_args()
    links = ["--wheelhouse", str(args.wheelhouse.resolve())] if args.wheelhouse else []
    output = args.output.resolve()
    (wheel,) = (output / "wheels").glob("toolframe-*.whl")
    env = {
        **os.environ,
        "UV_TOOL_DIR": str(output / "installed-tools"),
        "UV_TOOL_BIN_DIR": str(output / "bin"),
    }
    env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONPATH", None)
    subprocess.run(
        ["uv", "tool", "install", "--python", args.python, str(wheel)],
        env=env,
        check=True,
    )
    command = output / "bin/toolframe"
    assert (
        hashlib.sha256(command.read_bytes()).digest()
        == hashlib.sha256((output / "release/toolframe").read_bytes()).digest()
    )
    subprocess.run([str(command), "--version"], env=env, check=True)
    subprocess.run(
        [
            "uv",
            "tool",
            "run",
            "--isolated",
            "--python",
            args.python,
            "--from",
            str(wheel),
            "toolframe",
            "--version",
        ],
        env=env,
        check=True,
    )
    root = Path(__file__).resolve().parents[2]
    subprocess.run(
        [
            sys.executable,
            str(root / "scripts/verify-release.py"),
            *links,
            "--binary",
            str(command),
            "--architecture",
            args.architecture,
            "--output",
            str(output / "acceptance.json"),
        ],
        env=env,
        check=True,
    )
    baseline_env = {
        **env,
        "UV_TOOL_DIR": str(output / "baseline-tools"),
        "UV_TOOL_BIN_DIR": str(output / "baseline-bin"),
    }
    subprocess.run(
        ["uv", "tool", "install", "--python", args.python, "toolframe==0.8.0"],
        env=baseline_env,
        check=True,
    )
    subprocess.run(
        [
            "uv",
            "run",
            "--no-project",
            "--python",
            args.python,
            "--with",
            "httpx>=0.28,<1",
            "--with",
            "tomlkit>=0.13,<1",
            "python",
            str(root / "scripts/verify-retained-plugins.py"),
            *links,
            "--previous-binary",
            str(output / "baseline-bin/toolframe"),
            "--binary",
            str(command),
            "--runtime-root",
            str(output / "retained-runtime"),
            "--output",
            str(output / "retained.json"),
        ],
        env=env,
        check=True,
    )
    subprocess.run(
        [
            sys.executable,
            str(root / "scripts/verify-skill.py"),
            *links,
            "--binary",
            str(command),
            "--output",
            str(output / "skill.json"),
        ],
        env=env,
        check=True,
    )


if __name__ == "__main__":
    main()
