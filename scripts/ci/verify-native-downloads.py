"""Verify public release assets against signed provenance and this run's bundle."""

import argparse
import hashlib
import json
import platform
import re
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

TARGETS = {
    "aarch64-apple-darwin": ("Darwin", "arm64"),
    "x86_64-apple-darwin": ("Darwin", "x86_64"),
    "aarch64-unknown-linux-gnu": ("Linux", "aarch64"),
    "x86_64-unknown-linux-gnu": ("Linux", "x86_64"),
}
MAX_BYTES = 512 * 1024 * 1024
WORKFLOW = ".github/workflows/publish.yml"


def digest(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Expected a regular asset: {path.name}")
    if path.stat().st_size > MAX_BYTES:
        raise ValueError(f"Asset exceeds size limit: {path.name}")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path):
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise TypeError(f"Expected a JSON object: {path.name}")
    return value


def contract(bundle, repository, tag, commit, run_id, target):
    """Read expectations exclusively from the current run's trusted artifact."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid repository")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Invalid commit")
    if target not in TARGETS:
        raise ValueError("Unsupported target")
    candidate = read_json(bundle / "candidate.json")
    version = candidate["identity"]["version"]
    if (
        not re.fullmatch(r"[0-9A-Za-z.+-]+", version)
        or tag != f"v{version}"
        or candidate.get("schema") != 1
    ):
        raise ValueError("Release tag does not match the validated version")
    expected_producer = {
        "repository": repository,
        "ref": f"refs/tags/{tag}",
        "commit": commit,
        "run_id": run_id,
        "event": "workflow_dispatch",
    }
    if any(
        candidate["producer"].get(key) != value
        for key, value in expected_producer.items()
    ):
        raise ValueError("Comparison bundle is not from this publication run")
    prefix = f"launcher-release-{target}/"
    records = {}
    for name, record in candidate["files"].items():
        if not name.startswith(prefix):
            continue
        filename = name.removeprefix(prefix)
        if (
            PurePosixPath(filename).name != filename
            or filename in ("", ".", "..")
            or "\\" in filename
        ):
            raise ValueError("Unsafe release asset name")
        source = bundle / name
        if (
            digest(source) != record["sha256"]
            or source.stat().st_size != record["size"]
        ):
            raise ValueError("Comparison asset differs from its sealed hash")
        records[filename] = record["sha256"]
    manifest_name = f"manifest-{target}.json"
    manifest = read_json(bundle / prefix / manifest_name)
    if manifest.get("version") != version or manifest.get("target") != target:
        raise ValueError("Release manifest version or platform mismatch")
    archive = f"toolframe-{version}-{target}.tar.gz"
    sums = f"SHA256SUMS-{target}.txt"
    required = {archive, manifest_name, sums}
    companions = manifest.get("companions", [])
    if len(companions) != (1 if target == "x86_64-apple-darwin" else 0):
        raise ValueError("Incorrect companion set")
    for item in companions:
        filename = item["filename"]
        if (
            PurePosixPath(filename).name != filename
            or "\\" in filename
            or not filename.endswith(".whl")
            or item.get("url")
            != f"https://github.com/{repository}/releases/download/{tag}/{filename}"
        ):
            raise ValueError("Invalid companion identity")
        required.add(filename)
        if records.get(filename) != item["sha256"]:
            raise ValueError("Companion checksum mismatch")
    if set(records) != required:
        raise ValueError("Incomplete or unexpected release asset set")
    checksums = {}
    for line in (bundle / prefix / sums).read_text().splitlines():
        checksum, filename = line.split("  ", 1)
        if filename in checksums or records.get(filename) != checksum:
            raise ValueError("Invalid release checksum")
        checksums[filename] = checksum
    if set(checksums) != required - {manifest_name, sums}:
        raise ValueError("Incomplete release checksums")

    # Match the shell glob used to produce the public aggregate checksum file.
    combined = []
    for item in sorted(TARGETS):
        name = f"launcher-release-{item}/SHA256SUMS-{item}.txt"
        source = bundle / name
        if digest(source) != candidate["files"][name]["sha256"]:
            raise ValueError("Aggregate checksum source differs from its sealed hash")
        combined.append(source.read_bytes())
    records["SHA256SUMS"] = hashlib.sha256(b"".join(combined)).hexdigest()
    return manifest, records


def verify_assets(downloads, records, repository, tag, commit, run=subprocess.run):
    """Let gh verify signatures and provenance; never trust decoded payloads alone."""
    if {path.name for path in downloads.iterdir()} != set(records):
        raise ValueError("Downloaded release asset set differs")
    results = {}
    for filename, expected in sorted(records.items()):
        path = downloads / filename
        if digest(path) != expected:
            raise ValueError(f"Public asset checksum mismatch: {filename}")
        command = [
            "gh",
            "attestation",
            "verify",
            str(path),
            "--repo",
            repository,
            "--signer-workflow",
            f"{repository}/{WORKFLOW}",
            "--source-ref",
            f"refs/tags/{tag}",
            "--source-digest",
            commit,
            "--predicate-type",
            "https://slsa.dev/provenance/v1",
            "--deny-self-hosted-runners",
            "--format",
            "json",
        ]
        result = run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
        results[filename] = {
            "sha256": expected,
            "command": command,
            "verification": json.loads(result.stdout),
        }
    return results


def stage_binary(archive, manifest, output):
    """Read known regular members without performing filesystem extraction."""
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        if (
            len(members) != 3
            or {item.name for item in members} != {"toolframe", "LICENSE", "NOTICE"}
            or any(not item.isfile() for item in members)
            or any(item.size < 0 for item in members)
            or sum(item.size for item in members) > MAX_BYTES
        ):
            raise ValueError("Unsafe native archive members")
        binary = bundle.extractfile(bundle.getmember("toolframe")).read()
    if hashlib.sha256(binary).hexdigest() != manifest["binary_sha256"]:
        raise ValueError("Executable checksum differs from the release manifest")
    output.mkdir(parents=True, exist_ok=False)
    executable = output / "toolframe"
    executable.write_bytes(binary)
    executable.chmod(0o755)
    return executable


def check_platform(target):
    actual = (platform.system(), platform.machine().lower())
    if actual != TARGETS[target]:
        raise ValueError(f"Runner platform {actual} does not match {target}")
    return actual


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--target", choices=TARGETS, required=True)
    args = parser.parse_args()
    actual = check_platform(args.target)
    manifest, records = contract(
        args.bundle,
        args.repository,
        args.tag,
        args.commit,
        args.run_id,
        args.target,
    )
    args.output.mkdir(parents=True, exist_ok=False)
    downloads = args.output / "downloads"
    downloads.mkdir()
    command = [
        "gh",
        "release",
        "download",
        args.tag,
        "--repo",
        args.repository,
        "--dir",
        str(downloads),
    ]
    for filename in sorted(records):
        command.extend(["--pattern", filename])
    subprocess.run(command, check=True, timeout=300)
    evidence = {
        "repository": args.repository,
        "tag": args.tag,
        "commit": args.commit,
        "run_id": args.run_id,
        "workflow": WORKFLOW,
        "target": args.target,
        "runner_platform": list(actual),
        "runner_release": platform.release(),
        "runner_description": platform.platform(),
        "assets": verify_assets(
            downloads, records, args.repository, args.tag, args.commit
        ),
    }
    archive = downloads / (f"toolframe-{manifest['version']}-{args.target}.tar.gz")
    executable = stage_binary(archive, manifest, args.output / "bin")
    evidence["binary_sha256"] = digest(executable)
    evidence["passed"] = True
    (args.output / "download-verification.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    )


if __name__ == "__main__":
    main()
