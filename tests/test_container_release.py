"""Release staging rejects corrupted or mismatched executable archives."""

import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "prepare_container",
    Path(__file__).resolve().parents[1] / "scripts/prepare-container.py",
)
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)


def release(tmp_path, *, version="0.6.2", binary_hash=None, symlink=False):
    target = prepare.TARGETS["arm64"]
    archive = tmp_path / f"toolframe-{version}-{target}.tar.gz"
    binary = b"verified-executable"
    with tarfile.open(archive, "w:gz") as bundle:
        for name, data in (
            ("toolframe", binary),
            ("LICENSE", b"license"),
            ("NOTICE", b"notice"),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            if symlink and name == "toolframe":
                member.type = tarfile.SYMTYPE
                member.linkname = "/etc/passwd"
            bundle.addfile(member, io.BytesIO(data) if member.isfile() else None)
    (tmp_path / "SHA256SUMS").write_text(
        f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n"
    )
    (tmp_path / f"manifest-{target}.json").write_text(
        json.dumps(
            {
                "version": version,
                "target": target,
                "binary_sha256": binary_hash or hashlib.sha256(binary).hexdigest(),
            }
        )
    )
    return archive


def test_stages_verified_binary_and_licenses(tmp_path):
    release(tmp_path)
    output = tmp_path / "output"
    prepare.stage(tmp_path, output, "0.6.2", "arm64")
    assert (output / "arm64/toolframe").read_bytes() == b"verified-executable"
    assert (output / "arm64/toolframe").stat().st_mode & 0o777 == 0o755
    assert {path.name for path in (output / "arm64").iterdir()} == {
        "toolframe",
        "LICENSE",
        "NOTICE",
    }


def test_rejects_changed_archive(tmp_path):
    archive = release(tmp_path)
    archive.write_bytes(archive.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="Checksum mismatch"):
        prepare.stage(tmp_path, tmp_path / "output", "0.6.2", "arm64")


def test_rejects_wrong_binary_manifest(tmp_path):
    release(tmp_path, binary_hash="0" * 64)
    with pytest.raises(ValueError, match="Binary does not match"):
        prepare.stage(tmp_path, tmp_path / "output", "0.6.2", "arm64")


def test_rejects_wrong_architecture_manifest(tmp_path):
    release(tmp_path)
    manifest = tmp_path / f"manifest-{prepare.TARGETS['arm64']}.json"
    value = json.loads(manifest.read_text())
    value["target"] = prepare.TARGETS["amd64"]
    manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="manifest does not match"):
        prepare.stage(tmp_path, tmp_path / "output", "0.6.2", "arm64")


def test_does_not_extract_archive_symlinks(tmp_path):
    release(tmp_path, symlink=True)
    with pytest.raises(ValueError, match="regular file"):
        prepare.stage(tmp_path, tmp_path / "output", "0.6.2", "arm64")


def test_accepts_per_platform_ci_checksums(tmp_path):
    release(tmp_path)
    (tmp_path / "SHA256SUMS").rename(
        tmp_path / f"SHA256SUMS-{prepare.TARGETS['arm64']}.txt"
    )
    prepare.stage(tmp_path, tmp_path / "output", "0.6.2", "arm64")
