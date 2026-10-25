"""Seal validated distributions and promote them across trusted GitHub CI runs."""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = "validated-release"
MANIFEST = "candidate.json"
TARGETS = {
    "aarch64-apple-darwin": "macosx_12_0_arm64",
    "x86_64-apple-darwin": "macosx_12_0_x86_64",
    "aarch64-unknown-linux-gnu": "manylinux_2_28_aarch64",
    "x86_64-unknown-linux-gnu": "manylinux_2_28_x86_64",
}
MAX_BYTES = 512 * 1024 * 1024


def read_json(path):
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise TypeError(f"Expected a JSON object: {path.name}")
    return value


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def identity():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    rust = tomllib.loads((ROOT / "Cargo.toml").read_text())["package"]["version"]
    if version != rust or not re.fullmatch(r"[0-9A-Za-z.+-]+", version):
        raise ValueError("Core and launcher versions must match")
    return {
        "tree": git("rev-parse", "HEAD^{tree}"),
        "version": version,
        "dependency_owner": os.environ.get("TOOLFRAME_REPOSITORY_OWNER")
        or os.environ["GITHUB_REPOSITORY"].split("/")[0],
    }


def producer():
    commit = git("rev-parse", "HEAD")
    if commit != os.environ["GITHUB_SHA"] or git("status", "--porcelain"):
        raise ValueError("Artifacts require a clean checkout of GITHUB_SHA")
    event = read_json(Path(os.environ["GITHUB_EVENT_PATH"]))
    pr = event.get("pull_request", {})
    return {
        "repository": os.environ["GITHUB_REPOSITORY"],
        "run_id": int(os.environ["GITHUB_RUN_ID"]),
        "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
        "event": os.environ["GITHUB_EVENT_NAME"],
        "ref": os.environ["GITHUB_REF"],
        "commit": commit,
        "head_sha": pr.get("head", {}).get("sha", commit),
        "base_sha": pr.get("base", {}).get("sha"),
        "pull_request": pr.get("number"),
    }


def inventory(directory):
    files = {}
    total = 0
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("Artifact bundles cannot contain symbolic links")
        if not path.is_file() or path == directory / MANIFEST:
            continue
        size = path.stat().st_size
        total += size
        if total > MAX_BYTES or len(files) >= 500:
            raise ValueError("Artifact bundle exceeds size limits")
        with path.open("rb") as stream:
            files[path.relative_to(directory).as_posix()] = {
                "size": size,
                "sha256": hashlib.file_digest(stream, "sha256").hexdigest(),
            }
    return files


def check_layout(directory, version, build_source):
    expected = {"python-dist", "python-validation", "postgres-validation"}
    expected.update(
        f"launcher-{kind}-{target}"
        for target in TARGETS
        for kind in ("wheel", "release", "evidence")
    )
    actual = {path.name for path in directory.iterdir() if path.name != MANIFEST}
    if actual != expected or any(not (directory / name).is_dir() for name in expected):
        raise ValueError("Incomplete or unexpected validation artifact groups")
    core_files = {path.name for path in (directory / "python-dist").iterdir()}
    if core_files != {
        f"toolframe_core-{version}-py3-none-any.whl",
        f"toolframe_core-{version}.tar.gz",
    }:
        raise ValueError("Core distribution set or version does not match")
    for kind in ("python", "postgres"):
        report = read_json(directory / f"{kind}-validation/validation.json")
        if report.get("passed") is not True or report.get("checks") != kind:
            raise ValueError(f"Missing successful {kind} validation")
        if (
            report.get("core_commit") != build_source["commit"]
            or report.get("core_dirty") is not False
        ):
            raise ValueError(f"{kind} evidence is not for the validated clean commit")
    for target, platform_tag in TARGETS.items():
        release = directory / f"launcher-release-{target}"
        manifest = read_json(release / f"manifest-{target}.json")
        if manifest.get("target") != target or manifest.get("version") != version:
            raise ValueError(f"Launcher version/target mismatch: {target}")
        wheel_name = manifest["wheel"]
        if wheel_name != f"toolframe-{version}-py3-none-{platform_tag}.whl":
            raise ValueError("Invalid launcher wheel name")
        wheels = directory / f"launcher-wheel-{target}"
        if {path.name for path in wheels.iterdir()} != {wheel_name}:
            raise ValueError(f"Incomplete launcher wheel set: {target}")
        if inventory(wheels)[wheel_name]["sha256"] != manifest["wheel_sha256"]:
            raise ValueError(f"Launcher wheel checksum mismatch: {target}")
        required = {
            f"toolframe-{version}-{target}.tar.gz",
            f"manifest-{target}.json",
            f"SHA256SUMS-{target}.txt",
        }
        companions = manifest.get("companions", [])
        if len(companions) != (1 if target == "x86_64-apple-darwin" else 0):
            raise ValueError("Incorrect Intel companion release set")
        for item in companions:
            filename = item["filename"]
            url = f"https://github.com/toolframe/toolframe-core/releases/download/v{version}/{filename}"
            if (
                Path(filename).name != filename
                or not filename.endswith(".whl")
                or item.get("url") != url
            ):
                raise ValueError("Invalid companion filename or versioned URL")
        required.update(item["filename"] for item in companions)
        if {path.name for path in release.iterdir()} != required:
            raise ValueError(f"Incomplete launcher release set: {target}")
        hashes = inventory(release)
        sums = {}
        for line in (release / f"SHA256SUMS-{target}.txt").read_text().splitlines():
            digest, filename = line.split("  ", 1)
            if (
                filename in sums
                or filename not in hashes
                or hashes[filename]["sha256"] != digest
            ):
                raise ValueError(f"Invalid release checksum: {target}")
            sums[filename] = digest
        if set(sums) != required - {
            f"manifest-{target}.json",
            f"SHA256SUMS-{target}.txt",
        }:
            raise ValueError(f"Incomplete release checksums: {target}")
        for item in companions:
            if hashes[item["filename"]]["sha256"] != item["sha256"]:
                raise ValueError("Companion checksum mismatch")
        evidence = directory / f"launcher-evidence-{target}"
        for name in ("acceptance.json", "skill.json", "retained.json"):
            if not (evidence / name).is_file():
                raise ValueError(f"Missing installed launcher evidence: {target}")
            report = read_json(evidence / name)
            if report.get("binary_sha256") != manifest["binary_sha256"]:
                raise ValueError(
                    f"Installed evidence is for another executable: {target}"
                )
            if name != "acceptance.json" and report.get("passed") is not True:
                raise ValueError(f"Installed {name} acceptance did not pass: {target}")


def seal(directory, source, current):
    check_layout(directory, current["version"], source)
    manifest = {
        "schema": 1,
        "identity": current,
        "producer": source,
        "origins": [],
        "files": inventory(directory),
    }
    write_json(directory / MANIFEST, manifest)
    return manifest


def verify(directory, current):
    manifest = read_json(directory / MANIFEST)
    if manifest.get("schema") != 1 or manifest.get("identity") != current:
        raise ValueError("Artifact source tree, version or dependency owner differs")
    if not isinstance(manifest.get("producer"), dict) or not isinstance(
        manifest.get("origins"), list
    ):
        raise TypeError("Invalid artifact provenance")
    if manifest.get("files") != inventory(directory):
        raise ValueError("Artifact contents do not match their sealed hashes")
    original = (
        manifest["origins"][0]["producer"]
        if manifest["origins"]
        else manifest["producer"]
    )
    check_layout(directory, current["version"], original)
    return manifest


class GitHub:
    """Read only the calling repository; gh handles authenticated redirects."""

    def __init__(self, repository):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Invalid GitHub repository")
        self.repository = repository

    def command(self, endpoint, **kwargs):
        return subprocess.run(
            [
                "gh",
                "api",
                "--hostname",
                "github.com",
                "--method",
                "GET",
                f"repos/{self.repository}/{endpoint}",
            ],
            check=True,
            stderr=subprocess.PIPE,
            timeout=120,
            **kwargs,
        )

    def get(self, endpoint):
        return json.loads(self.command(endpoint, stdout=subprocess.PIPE).stdout)

    def runs(self, sha):
        query = urlencode({"head_sha": sha, "per_page": 100})
        return self.get(f"actions/workflows/ci.yml/runs?{query}")["workflow_runs"]

    def download(self, artifact, destination):
        with destination.open("wb") as stream:
            self.command(f"actions/artifacts/{int(artifact['id'])}/zip", stdout=stream)
        if destination.stat().st_size > MAX_BYTES:
            raise ValueError("Artifact archive exceeds size limit")
        with destination.open("rb") as stream:
            digest = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
        if artifact.get("digest") != digest:
            raise ValueError("GitHub artifact archive digest mismatch")


def trusted_run(run, repository):
    return (
        (run.get("repository") or {}).get("full_name") == repository
        and (run.get("head_repository") or {}).get("full_name") == repository
        and run.get("path", "").split("@", 1)[0] == ".github/workflows/ci.yml"
    )


def latest(runs):
    return max(runs, key=lambda run: (run["run_started_at"], run["id"]), default=None)


def select_run(api, stage, source):
    runs = [
        run
        for run in api.runs(source["commit"])
        if run["id"] != source["run_id"]
        and trusted_run(run, source["repository"])
        and run["head_branch"] == "main"
        and run["event"] in ("push", "workflow_dispatch")
    ]
    if run := latest(runs):
        # A newer failed/incomplete validation must not be hidden by an old green run.
        return run, None
    if stage != "main":
        return None, None
    prs = api.get(f"commits/{source['commit']}/pulls?per_page=100")
    for item in prs:
        if item.get("merge_commit_sha") != source["commit"]:
            continue
        pr = api.get(f"pulls/{int(item['number'])}")
        if (
            not pr.get("merged")
            or pr["merge_commit_sha"] != source["commit"]
            or pr["base"]["ref"] != "main"
            or (pr["head"].get("repo") or {}).get("full_name") != source["repository"]
        ):
            continue
        runs = [
            run
            for run in api.runs(pr["head"]["sha"])
            if trusted_run(run, source["repository"]) and run["event"] == "pull_request"
        ]
        return latest(runs), pr
    return None, None


def unpack(archive, destination):
    with zipfile.ZipFile(archive) as bundle:
        names = set()
        total = 0
        for item in bundle.infolist():
            name = item.filename
            path = PurePosixPath(name.rstrip("/"))
            total += item.file_size
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in name
                or str(path) != name.rstrip("/")
                or name in names
                or stat.S_ISLNK(item.external_attr >> 16)
                or total > MAX_BYTES
                or len(names) >= 500
            ):
                raise ValueError("Unsafe artifact archive entry")
            names.add(name)
            if item.is_dir():
                continue
            target = destination / path
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(item) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)


def check_producer(api, manifest, run, pr, current):
    source = manifest["producer"]
    expected = {
        "repository": api.repository,
        "run_id": run["id"],
        "run_attempt": run["run_attempt"],
        "event": run["event"],
        "head_sha": run["head_sha"],
    }
    if any(source.get(key) != value for key, value in expected.items()):
        raise ValueError("Artifact producer differs from the GitHub run")
    if not re.fullmatch(r"[0-9a-f]{40}", source["commit"]):
        raise ValueError("Invalid producer commit")
    commit = api.get(f"git/commits/{source['commit']}")
    if commit["tree"]["sha"] != current["tree"]:
        raise ValueError("GitHub producer commit has a different source tree")
    if pr:
        if (
            source["pull_request"] != pr["number"]
            or source["ref"] != f"refs/pull/{pr['number']}/merge"
            or [parent["sha"] for parent in commit["parents"]]
            != [source["base_sha"], pr["head"]["sha"]]
        ):
            raise ValueError("Artifact was not built from this PR merge commit")
    elif source["commit"] != run["head_sha"] or source["ref"] != "refs/heads/main":
        raise ValueError("Artifact was not validated on main")


def reuse(api, stage, output, source, current):
    if stage == "main":
        if source["ref"] != "refs/heads/main" or source["event"] not in (
            "push",
            "workflow_dispatch",
        ):
            raise ValueError("Main promotion requires a main CI run")
    elif (
        stage != "release"
        or source["ref"] != f"refs/tags/v{current['version']}"
        or source["event"] != "workflow_dispatch"
    ):
        raise ValueError("Release promotion requires the matching manual tag run")
    run, pr = select_run(api, stage, source)
    if not run or run["status"] != "completed" or run["conclusion"] != "success":
        print("No successful latest CI source; running complete validation")
        return False
    artifacts = api.get(f"actions/runs/{run['id']}/artifacts?per_page=100")["artifacts"]
    candidates = [
        item for item in artifacts if item["name"] == ARTIFACT and not item["expired"]
    ]
    if len(candidates) != 1:
        print("No retained validated bundle; running complete validation")
        return False
    artifact = candidates[0]
    with tempfile.TemporaryDirectory(prefix="toolframe-artifacts-") as temporary:
        root = Path(temporary)
        archive = root / "artifact.zip"
        api.download(artifact, archive)
        bundle = root / "bundle"
        bundle.mkdir()
        unpack(archive, bundle)
        manifest = verify(bundle, current)
        # A rerun may have started while the artifact was downloading. Do not
        # accept stale success or an artifact belonging to an earlier attempt.
        run = api.get(f"actions/runs/{run['id']}")
        if run["status"] != "completed" or run["conclusion"] != "success":
            raise ValueError("Source validation changed during download")
        check_producer(api, manifest, run, pr, current)
        manifest["origins"].append(
            {
                "producer": manifest["producer"],
                "artifact_id": artifact["id"],
                "artifact_digest": artifact["digest"],
            }
        )
        manifest["producer"] = source
        write_json(bundle / MANIFEST, manifest)
        shutil.copytree(bundle, output)
    print(
        f"Reused verified artifacts from CI run {run['id']} (artifact {artifact['id']})"
    )
    return True


def export(directory, output, kind, target, current, source):
    manifest = verify(directory, current)
    producer = manifest["producer"]
    if any(
        producer[key] != source[key]
        for key in ("repository", "run_id", "commit", "ref")
    ):
        raise ValueError("Publication requires the bundle validated in this run")
    if kind == "core":
        groups = [directory / "python-dist"]
    else:
        targets = [target] if target else TARGETS
        groups = [directory / f"launcher-{kind}-{item}" for item in targets]
    output.mkdir(parents=True, exist_ok=False)
    for group in groups:
        for file in group.iterdir():
            destination = output / file.name
            if destination.exists():
                raise ValueError("Duplicate publication filename")
            shutil.copyfile(file, destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pack = commands.add_parser("seal")
    pack.add_argument("--bundle", type=Path, required=True)
    resolve = commands.add_parser("resolve")
    resolve.add_argument("--stage", choices=("none", "main", "release"), required=True)
    resolve.add_argument("--output", type=Path, required=True)
    resolve.add_argument("--rebuild", action="store_true")
    publish = commands.add_parser("export")
    publish.add_argument("--bundle", type=Path, required=True)
    publish.add_argument("--output", type=Path, required=True)
    publish.add_argument("--kind", choices=("core", "wheel", "release"), required=True)
    publish.add_argument("--target", choices=TARGETS)
    args = parser.parse_args()
    current, source = identity(), producer()
    if args.command == "seal":
        seal(args.bundle, source, current)
    elif args.command == "export":
        export(args.bundle, args.output, args.kind, args.target, current, source)
    else:
        reused = False
        if args.stage != "none" and not args.rebuild and source["run_attempt"] == 1:
            try:
                reused = reuse(
                    GitHub(source["repository"]),
                    args.stage,
                    args.output,
                    source,
                    current,
                )
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                zipfile.BadZipFile,
                subprocess.SubprocessError,
            ) as error:
                # A reuse failure cannot turn into a green CI result. The caller
                # must successfully run all original validation jobs instead.
                print(
                    f"Cannot reuse validation ({type(error).__name__}: {error}); rebuilding"
                )
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            stream.write(f"reused={str(reused).lower()}\n")
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
            stream.write(
                "Reused a verified validation bundle; see provenance in `candidate.json`.\n"
                if reused
                else "No reusable bundle selected. Complete validation is required.\n"
            )


if __name__ == "__main__":
    main()
