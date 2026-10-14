"""Verify Linux release archives and stage the minimal Docker build context."""

import argparse
import hashlib
import json
import re
import tarfile
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {"amd64": "x86_64-unknown-linux-gnu", "arm64": "aarch64-unknown-linux-gnu"}


def stage(releases, output, version, architecture):
    target = TARGETS[architecture]
    archive = releases / f"toolframe-{version}-{target}.tar.gz"
    sums = releases / f"SHA256SUMS-{target}.txt"
    if not sums.exists():
        sums = releases / "SHA256SUMS"
    checksums = dict(
        line.split(maxsplit=1)[::-1] for line in sums.read_text().splitlines()
    )
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if checksums.get(archive.name) != digest:
        raise ValueError(f"Checksum mismatch: {archive.name}")
    manifest = json.loads((releases / f"manifest-{target}.json").read_text())
    if manifest["version"] != version or manifest["target"] != target:
        raise ValueError(f"Release manifest does not match {version}/{target}")
    destination = output / architecture
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as bundle:
        # Read only known regular members; never extract paths or symlinks from an archive.
        for name in ("toolframe", "LICENSE", "NOTICE"):
            member = bundle.getmember(name)
            if not member.isfile():
                raise ValueError(f"Expected a regular file: {name}")
            data = bundle.extractfile(member).read()
            if (
                name == "toolframe"
                and hashlib.sha256(data).hexdigest() != manifest["binary_sha256"]
            ):
                raise ValueError("Binary does not match the release manifest")
            (destination / name).write_bytes(data)
            (destination / name).chmod(0o755 if name == "toolframe" else 0o644)
    print(f"Verified {archive.name}: {digest}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--release-dir",
        type=Path,
        help="Use existing CI artifacts; otherwise download a release",
    )
    parser.add_argument("--output", type=Path, default=ROOT / "build/container")
    parser.add_argument(
        "--version",
        default=tomllib.loads((ROOT / "Cargo.toml").read_text())["package"]["version"],
    )
    parser.add_argument("--repository", default="toolframe/toolframe-core")
    parser.add_argument("--architecture", choices=TARGETS, action="append")
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", args.version):
        parser.error("Expected a semantic release version without the v prefix")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        parser.error("Expected owner/repository")
    architectures = args.architecture or list(TARGETS)
    releases = args.release_dir or ROOT / "build/container-downloads" / args.version
    if args.release_dir is None:
        releases.mkdir(parents=True, exist_ok=True)
        names = ["SHA256SUMS"]
        for arch in architectures:
            target = TARGETS[arch]
            names.extend(
                [f"toolframe-{args.version}-{target}.tar.gz", f"manifest-{target}.json"]
            )
        for name in names:
            url = f"https://github.com/{args.repository}/releases/download/v{args.version}/{name}"
            with urllib.request.urlopen(url, timeout=60) as response:
                (releases / name).write_bytes(response.read())
    for arch in architectures:
        stage(releases, args.output, args.version, arch)


if __name__ == "__main__":
    main()
