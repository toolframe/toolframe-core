"""Promotion must preserve tested bytes and reject untrusted or stale evidence."""

import hashlib
import importlib.util
import itertools
import json
import os
import shutil
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "ci_artifacts", ROOT / "scripts/ci/artifacts.py"
)
artifacts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(artifacts)
REPOSITORY = "toolframe/toolframe-core"
IDENTITY = {"tree": "a" * 40, "version": "1.2.3", "dependency_owner": "toolframe"}
PR_SOURCE = {
    "repository": REPOSITORY,
    "run_id": 101,
    "run_attempt": 1,
    "event": "pull_request",
    "ref": "refs/pull/7/merge",
    "commit": "b" * 40,
    "head_sha": "c" * 40,
    "base_sha": "e" * 40,
    "pull_request": 7,
}
MAIN_SOURCE = {
    **PR_SOURCE,
    "run_id": 102,
    "event": "push",
    "ref": "refs/heads/main",
    "commit": "d" * 40,
    "head_sha": "d" * 40,
    "base_sha": None,
    "pull_request": None,
}
RELEASE_SOURCE = {
    **MAIN_SOURCE,
    "run_id": 103,
    "event": "workflow_dispatch",
    "ref": "refs/tags/v1.2.3",
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, dict):
        value = json.dumps(value).encode()
    path.write_bytes(value)


@pytest.fixture
def bundle(tmp_path):
    directory = tmp_path / "bundle"
    for filename in (
        "toolframe_core-1.2.3-py3-none-any.whl",
        "toolframe_core-1.2.3.tar.gz",
    ):
        write(directory / "python-dist" / filename, filename.encode())
    for kind in ("python", "postgres"):
        write(
            directory / f"{kind}-validation/validation.json",
            {
                "passed": True,
                "checks": kind,
                "core_commit": PR_SOURCE["commit"],
                "core_dirty": False,
            },
        )
    for target, tag in artifacts.TARGETS.items():
        wheel = f"toolframe-1.2.3-py3-none-{tag}.whl"
        write(directory / f"launcher-wheel-{target}" / wheel, target.encode())
        release = directory / f"launcher-release-{target}"
        archive = f"toolframe-1.2.3-{target}.tar.gz"
        write(release / archive, target.encode())
        companions = []
        sums = f"{digest(target.encode())}  {archive}\n"
        if target == "x86_64-apple-darwin":
            filename = "cryptography-1.0-cp314-abi3-macosx_12_0_x86_64.whl"
            write(release / filename, b"companion wheel")
            companions.append(
                {
                    "filename": filename,
                    "sha256": digest(b"companion wheel"),
                    "url": f"https://github.com/toolframe/toolframe-core/releases/download/v1.2.3/{filename}",
                }
            )
            sums += f"{digest(b'companion wheel')}  {filename}\n"
        write(
            release / f"manifest-{target}.json",
            {
                "target": target,
                "version": "1.2.3",
                "wheel": wheel,
                "wheel_sha256": digest(target.encode()),
                "binary_sha256": digest(target.encode()),
                "companions": companions,
            },
        )
        write(
            release / f"SHA256SUMS-{target}.txt",
            sums.encode(),
        )
        for name in ("acceptance.json", "skill.json", "retained.json"):
            write(
                directory / f"launcher-evidence-{target}" / name,
                {"binary_sha256": digest(target.encode()), "passed": True},
            )
    artifacts.seal(directory, PR_SOURCE, IDENTITY)
    return directory


def run(source, **overrides):
    return {
        "id": source["run_id"],
        "run_attempt": source["run_attempt"],
        "repository": {"full_name": REPOSITORY},
        "head_repository": {"full_name": REPOSITORY},
        "head_sha": source["head_sha"],
        "head_branch": "main"
        if source["ref"] == "refs/heads/main"
        else "chore/example",
        "event": source["event"],
        "path": ".github/workflows/ci.yml",
        "status": "completed",
        "conclusion": "success",
        "run_started_at": "2026-10-15T00:00:00Z",
        **overrides,
    }


class GitHubFixture:
    repository = REPOSITORY

    def __init__(self, bundle, source=PR_SOURCE):
        self.bundle = bundle
        self.source = source
        self.run = run(source)
        self.extra_runs = []
        self.pr = {
            "number": 7,
            "merged": True,
            "merge_commit_sha": MAIN_SOURCE["commit"],
            "base": {"ref": "main"},
            "head": {"sha": PR_SOURCE["head_sha"], "repo": {"full_name": REPOSITORY}},
        }
        self.artifact = {
            "id": 201,
            "name": artifacts.ARTIFACT,
            "expired": False,
            "digest": "sha256:fixture",
        }
        self.commit = {
            "tree": {"sha": IDENTITY["tree"]},
            "parents": [{"sha": PR_SOURCE["base_sha"]}, {"sha": PR_SOURCE["head_sha"]}],
        }
        self.downloads = 0

    def runs(self, sha):
        return [
            item for item in [self.run, *self.extra_runs] if item["head_sha"] == sha
        ]

    def get(self, endpoint):
        if endpoint.startswith("commits/"):
            return [self.pr]
        if endpoint == "pulls/7":
            return self.pr
        if endpoint.startswith("actions/runs/") and "/artifacts?" in endpoint:
            return {"artifacts": [self.artifact]}
        if endpoint == f"actions/runs/{self.run['id']}":
            return self.run
        if endpoint.startswith("git/commits/"):
            return self.commit
        raise AssertionError(endpoint)

    def download(self, artifact, destination):
        self.downloads += 1
        with zipfile.ZipFile(destination, "w") as archive:
            for path in self.bundle.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(self.bundle).as_posix())


def test_pr_main_release_preserves_every_tested_byte(bundle, tmp_path):
    original = artifacts.verify(bundle, IDENTITY)
    main = tmp_path / "main"
    assert artifacts.reuse(GitHubFixture(bundle), "main", main, MAIN_SOURCE, IDENTITY)
    promoted = artifacts.verify(main, IDENTITY)
    assert promoted["files"] == original["files"]
    assert promoted["producer"] == MAIN_SOURCE
    assert promoted["origins"][0]["producer"] == PR_SOURCE
    release = tmp_path / "release"
    assert artifacts.reuse(
        GitHubFixture(main, MAIN_SOURCE), "release", release, RELEASE_SOURCE, IDENTITY
    )
    published = artifacts.verify(release, IDENTITY)
    assert published["files"] == original["files"]
    assert [item["producer"]["run_id"] for item in published["origins"]] == [101, 102]
    for kind, count in (("core", 2), ("wheel", 4), ("release", 13)):
        output = tmp_path / f"export-{kind}"
        artifacts.export(release, output, kind, None, IDENTITY, RELEASE_SOURCE)
        assert len(list(output.iterdir())) == count
        for file in output.iterdir():
            assert (
                file.read_bytes()
                == next(path for path in bundle.rglob(file.name)).read_bytes()
            )
    # Container publication consumes only its architecture from the same bundle.
    target = "aarch64-unknown-linux-gnu"
    output = tmp_path / "container"
    artifacts.export(release, output, "release", target, IDENTITY, RELEASE_SOURCE)
    assert len(list(output.iterdir())) == 3
    assert (output / f"toolframe-1.2.3-{target}.tar.gz").exists()


@pytest.mark.parametrize(
    "field,value",
    [("tree", "f" * 40), ("version", "1.2.4"), ("dependency_owner", "other")],
)
def test_changed_inputs_cannot_reuse(bundle, tmp_path, field, value):
    with pytest.raises(ValueError, match="source tree, version or dependency owner"):
        artifacts.reuse(
            GitHubFixture(bundle),
            "main",
            tmp_path / "out",
            MAIN_SOURCE,
            {**IDENTITY, field: value},
        )
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("change", ["tamper", "missing", "extra", "symlink"])
def test_file_integrity_cannot_be_bypassed(bundle, change):
    file = bundle / "python-dist/toolframe_core-1.2.3-py3-none-any.whl"
    if change == "tamper":
        file.write_bytes(b"different wheel")
    elif change == "missing":
        file.unlink()
    elif change == "extra":
        (bundle / "python-dist/extra.whl").write_bytes(b"unexpected")
    else:
        file.unlink()
        file.symlink_to(bundle / artifacts.MANIFEST)
    with pytest.raises(ValueError, match="hashes|symbolic links"):
        artifacts.verify(bundle, IDENTITY)


@pytest.mark.parametrize(
    "change",
    [
        "python",
        "postgres",
        "native",
        "wheel",
        "checksums",
        "target",
        "evidence",
        "retained_missing",
        "retained_failed",
        "stale",
        "dirty",
    ],
)
def test_sealing_requires_all_successful_platforms(bundle, change):
    target = "x86_64-apple-darwin"
    if change in ("python", "postgres"):
        write(
            bundle / f"{change}-validation/validation.json",
            {"passed": False, "checks": change},
        )
    elif change == "native":
        shutil.rmtree(bundle / f"launcher-release-{target}")
    elif change == "wheel":
        next((bundle / f"launcher-wheel-{target}").iterdir()).write_bytes(b"different")
    elif change == "checksums":
        (bundle / f"launcher-release-{target}/SHA256SUMS-{target}.txt").write_text("")
    elif change == "target":
        path = bundle / f"launcher-release-{target}/manifest-{target}.json"
        data = json.loads(path.read_text())
        data["target"] = "aarch64-apple-darwin"
        write(path, data)
    elif change in ("stale", "dirty"):
        path = bundle / "python-validation/validation.json"
        data = json.loads(path.read_text())
        data["core_commit" if change == "stale" else "core_dirty"] = (
            "f" * 40 if change == "stale" else True
        )
        write(path, data)
    elif change == "retained_missing":
        (bundle / f"launcher-evidence-{target}/retained.json").unlink()
    elif change == "retained_failed":
        write(
            bundle / f"launcher-evidence-{target}/retained.json",
            {"binary_sha256": digest(target.encode()), "passed": False},
        )
    else:
        write(bundle / f"launcher-evidence-{target}/skill.json", {"passed": False})
    with pytest.raises(ValueError):
        artifacts.seal(bundle, PR_SOURCE, IDENTITY)


@pytest.mark.parametrize(
    "condition",
    [
        "failed",
        "cancelled",
        "running",
        "expired",
        "fork",
        "unmerged",
        "wrong_merge",
        "wrong_workflow",
        "wrong_base",
    ],
)
def test_ineligible_sources_fall_back_without_downloading(bundle, tmp_path, condition):
    api = GitHubFixture(bundle)
    if condition == "failed":
        api.run["conclusion"] = "failure"
    elif condition == "cancelled":
        api.run["conclusion"] = "cancelled"
    elif condition == "running":
        api.run["status"] = "in_progress"
    elif condition == "expired":
        api.artifact["expired"] = True
    elif condition == "fork":
        api.pr["head"]["repo"]["full_name"] = "outsider/toolframe-core"
    elif condition == "unmerged":
        api.pr["merged"] = False
    elif condition == "wrong_merge":
        api.pr["merge_commit_sha"] = "f" * 40
    elif condition == "wrong_workflow":
        api.run["path"] = ".github/workflows/other.yml"
    else:
        api.pr["base"]["ref"] = "other"
    assert not artifacts.reuse(api, "main", tmp_path / "out", MAIN_SOURCE, IDENTITY)
    assert api.downloads == 0


def test_newer_failure_cannot_be_hidden_by_old_green_pr(bundle, tmp_path):
    api = GitHubFixture(bundle)
    api.extra_runs.append(
        run(
            PR_SOURCE,
            id=110,
            conclusion="failure",
            run_started_at="2026-10-15T01:00:00Z",
        )
    )
    assert not artifacts.reuse(api, "main", tmp_path / "out", MAIN_SOURCE, IDENTITY)
    assert api.downloads == 0


def test_failed_main_cannot_fall_back_to_green_pr(bundle, tmp_path):
    api = GitHubFixture(bundle)
    api.extra_runs.append(run(MAIN_SOURCE, id=110, conclusion="failure"))
    assert not artifacts.reuse(api, "main", tmp_path / "out", MAIN_SOURCE, IDENTITY)
    assert api.downloads == 0


def test_rerun_started_during_download_invalidates_prior_success(
    bundle, tmp_path, monkeypatch
):
    api = GitHubFixture(bundle)
    download = api.download

    def rerun(artifact, destination):
        download(artifact, destination)
        api.run.update(status="in_progress", conclusion=None, run_attempt=2)

    monkeypatch.setattr(api, "download", rerun)
    with pytest.raises(ValueError, match="changed during download"):
        artifacts.reuse(api, "main", tmp_path / "out", MAIN_SOURCE, IDENTITY)
    assert not (tmp_path / "out").exists()


def test_release_never_promotes_pr_directly(bundle, tmp_path):
    api = GitHubFixture(bundle)
    assert not artifacts.reuse(
        api, "release", tmp_path / "out", RELEASE_SOURCE, IDENTITY
    )
    assert api.downloads == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", 999),
        ("run_attempt", 2),
        ("repository", "other/repo"),
        ("head_sha", "f" * 40),
        ("ref", "refs/pull/8/merge"),
        ("pull_request", 8),
    ],
)
def test_self_reported_provenance_must_match_server(bundle, tmp_path, field, value):
    path = bundle / artifacts.MANIFEST
    data = json.loads(path.read_text())
    data["producer"][field] = value
    write(path, data)
    with pytest.raises(ValueError, match="producer|PR merge"):
        artifacts.reuse(
            GitHubFixture(bundle), "main", tmp_path / "out", MAIN_SOURCE, IDENTITY
        )


def test_server_commit_tree_and_merge_parents_are_verified(bundle, tmp_path):
    api = GitHubFixture(bundle)
    api.commit["tree"]["sha"] = "f" * 40
    with pytest.raises(ValueError, match="different source tree"):
        artifacts.reuse(api, "main", tmp_path / "out", MAIN_SOURCE, IDENTITY)
    api.commit["tree"]["sha"] = IDENTITY["tree"]
    api.commit["parents"] = []
    with pytest.raises(ValueError, match="PR merge"):
        artifacts.reuse(api, "main", tmp_path / "out", MAIN_SOURCE, IDENTITY)


@pytest.mark.parametrize(
    "name", ["../escape", "/absolute", "a/../../escape", "a\\escape", "a//b", "./file"]
)
def test_archive_paths_cannot_escape(tmp_path, name):
    archive = tmp_path / "input.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(name, "data")
    with pytest.raises(ValueError, match="Unsafe"):
        artifacts.unpack(archive, tmp_path / "out")


def test_archive_symlinks_and_duplicate_names_are_rejected(tmp_path):
    archive = tmp_path / "input.zip"
    info = zipfile.ZipInfo("link")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(info, "../escape")
    with pytest.raises(ValueError, match="Unsafe"):
        artifacts.unpack(archive, tmp_path / "out")
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("file", "one")
        with pytest.warns(UserWarning, match="Duplicate name"):
            output.writestr("file", "two")
    with pytest.raises(ValueError, match="Unsafe"):
        artifacts.unpack(archive, tmp_path / "out")


def test_download_requires_the_github_archive_digest(tmp_path, monkeypatch):
    api = artifacts.GitHub(REPOSITORY)

    def command(endpoint, **kwargs):
        kwargs["stdout"].write(b"downloaded bytes")

    monkeypatch.setattr(api, "command", command)
    spec = {"id": 201, "digest": "sha256:" + digest(b"downloaded bytes")}
    api.download(spec, tmp_path / "valid.zip")
    spec["digest"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="archive digest"):
        api.download(spec, tmp_path / "invalid.zip")


def test_export_rejects_a_different_run(bundle, tmp_path):
    with pytest.raises(ValueError, match="this run"):
        artifacts.export(bundle, tmp_path / "out", "core", None, IDENTITY, MAIN_SOURCE)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    "payload", [[], {"schema": 1, "identity": IDENTITY, "producer": [], "origins": []}]
)
def test_malformed_manifest_is_a_handled_reuse_error(bundle, payload):
    (bundle / artifacts.MANIFEST).write_text(json.dumps(payload))
    with pytest.raises(TypeError, match="JSON object|provenance"):
        artifacts.verify(bundle, IDENTITY)


@pytest.mark.parametrize(
    "stage,rebuild,attempt", [("none", False, 1), ("main", True, 1), ("main", False, 2)]
)
def test_explicit_rebuild_and_rerun_skip_lookup(
    tmp_path, monkeypatch, stage, rebuild, attempt
):
    monkeypatch.setattr(artifacts, "identity", lambda: IDENTITY)
    monkeypatch.setattr(
        artifacts, "producer", lambda: {**MAIN_SOURCE, "run_attempt": attempt}
    )
    monkeypatch.setattr(
        artifacts, "reuse", lambda *args: pytest.fail("Must run fresh checks")
    )
    args = [
        "artifacts.py",
        "resolve",
        "--stage",
        stage,
        "--output",
        str(tmp_path / "out"),
    ]
    if rebuild:
        args.append("--rebuild")
    monkeypatch.setattr("sys.argv", args)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    artifacts.main()
    assert (tmp_path / "output").read_text() == "reused=false\n"


def test_lookup_error_requires_full_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "identity", lambda: IDENTITY)
    monkeypatch.setattr(artifacts, "producer", lambda: MAIN_SOURCE)

    def unavailable(*args):
        raise OSError("GitHub temporarily unavailable")

    monkeypatch.setattr(artifacts, "reuse", unavailable)
    monkeypatch.setattr(
        "sys.argv",
        [
            "artifacts.py",
            "resolve",
            "--stage",
            "main",
            "--output",
            str(tmp_path / "out"),
        ],
    )
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    artifacts.main()
    assert (tmp_path / "output").read_text() == "reused=false\n"


def test_validation_gate_rejects_all_incomplete_outcomes():
    workflow = yaml.safe_load((ROOT / ".github/workflows/validate.yml").read_text())
    script = workflow["jobs"]["result"]["steps"][0]["run"]
    keys = ("PYTHON_RESULT", "POSTGRES_RESULT", "LAUNCHER_RESULT", "BUNDLE_RESULT")
    values = ("success", "failure", "cancelled", "skipped")
    # Each individual prerequisite must reject failure/cancellation/skipping on
    # a fresh path, and a reused path must not conceal a failed build either.
    for reused, expected in (("true", "skipped"), ("false", "success")):
        base = {
            **os.environ,
            "REUSED": reused,
            "REUSE_RESULT": "success",
            **dict.fromkeys(keys, expected),
        }
        assert (
            subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c", script], env=base, check=False
            ).returncode
            == 0
        )
        for key, value in itertools.product((*keys, "REUSE_RESULT"), values):
            env = {**base, key: value}
            result = subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c", script], env=env, check=False
            )
            assert (result.returncode == 0) == (value == base[key]), (
                reused,
                key,
                value,
            )
