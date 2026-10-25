#!/usr/bin/env python3
"""Check official template summaries against explicitly supplied release wheels."""

import argparse
import json
from pathlib import Path

from toolframe_core.authoring_metadata import from_wheel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "src/toolframe_core/authoring_catalog.json",
    )
    args = parser.parse_args()
    catalog = json.loads(args.catalog.read_text())
    wheels = {}
    for path in args.wheelhouse.glob("*.whl"):
        dist = from_wheel(path, required=False)
        if dist:
            if dist.distribution in wheels:
                raise ValueError(f"Ambiguous wheel for {dist.distribution}")
            wheels[dist.distribution] = dist
    for package in catalog["packages"]:
        dist = wheels.get(package["distribution"])
        if not dist:
            raise ValueError(f"Supply the matching {package['distribution']} wheel")
        summaries = [
            {key: row[key] for key in ("id", "title", "description")}
            for row in dist.templates()
        ]
        if summaries != package["templates"]:
            raise ValueError(
                f"Catalog differs from {dist.distribution}=={dist.version}"
            )
    print(f"Catalog matches {len(catalog['packages'])} distribution manifests")


if __name__ == "__main__":
    main()
