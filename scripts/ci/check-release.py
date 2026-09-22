"""Reject publishing unless the selected Git tag matches both package versions."""

import argparse
import tomllib
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", required=True, help="Full refs/tags/vVERSION ref")
    args = parser.parse_args()
    core = Path(__file__).resolve().parents[2]
    version = tomllib.loads((core / "pyproject.toml").read_text())["project"]["version"]
    rust_version = tomllib.loads((core / "Cargo.toml").read_text())["package"][
        "version"
    ]
    if version != rust_version or args.ref != f"refs/tags/v{version}":
        parser.error(
            f"Publishing requires refs/tags/v{version}; "
            f"received {args.ref!r}, Rust version {rust_version!r}"
        )
    print(f"Release tag and Python/Rust versions match: v{version}")


if __name__ == "__main__":
    main()
