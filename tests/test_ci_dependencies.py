"""Release source distributions retain their pinned identity and extraction boundary."""

import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "ci_validate", Path(__file__).resolve().parents[1] / "scripts/ci/validate.py"
)
validation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validation)


def sdist(tmp_path, *, version="0.1.0", member="pyproject.toml", name="toolframe-web"):
    content = f'[project]\nname = "{name}"\nversion = "{version}"\n'.encode()
    archive = tmp_path / "toolframe_web-0.1.0.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        info = tarfile.TarInfo(f"toolframe_web-0.1.0/{member}")
        info.size = len(content)
        bundle.addfile(info, io.BytesIO(content))
    pin = {
        "version": "0.1.0",
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }
    root = tmp_path / "build"
    (root / "workspace").mkdir(parents=True)
    return archive, pin, root


def test_supplied_sdist_matches_published_pin(tmp_path, monkeypatch):
    archive, pin, root = sdist(tmp_path)
    monkeypatch.setenv("TOOLFRAME_CI_SDIST_DIR", str(archive.parent))
    validation.prepare_sdist(root, "toolframe-web", pin)
    assert (root / "workspace/toolframe-web/pyproject.toml").is_file()


def test_corrupt_source_is_rejected_before_extraction(tmp_path, monkeypatch):
    archive, pin, root = sdist(tmp_path)
    archive.write_bytes(archive.read_bytes() + b"changed")
    monkeypatch.setenv("TOOLFRAME_CI_SDIST_DIR", str(archive.parent))
    with pytest.raises(ValueError, match="hash mismatch"):
        validation.prepare_sdist(root, "toolframe-web", pin)
    assert not (root / "sdists").exists()


@pytest.mark.parametrize(
    "name,version", [("another-package", "0.1.0"), ("toolframe-web", "0.2.0")]
)
def test_source_metadata_must_match_pinned_identity(
    tmp_path, monkeypatch, name, version
):
    archive, pin, root = sdist(tmp_path, name=name, version=version)
    monkeypatch.setenv("TOOLFRAME_CI_SDIST_DIR", str(archive.parent))
    with pytest.raises(ValueError, match="identity mismatch"):
        validation.prepare_sdist(root, "toolframe-web", pin)
    assert not (root / "workspace/toolframe-web").exists()


def test_source_cannot_escape_extraction_directory(tmp_path, monkeypatch):
    archive, pin, root = sdist(tmp_path, member="../../escape.toml")
    monkeypatch.setenv("TOOLFRAME_CI_SDIST_DIR", str(archive.parent))
    with pytest.raises(tarfile.FilterError):
        validation.prepare_sdist(root, "toolframe-web", pin)
    assert not (root / "sdists/escape.toml").exists()


def test_changed_index_hash_is_rejected_without_downloading_source(
    tmp_path, monkeypatch
):
    _, pin, root = sdist(tmp_path)
    monkeypatch.delenv("TOOLFRAME_CI_SDIST_DIR", raising=False)
    calls = []

    def request(url, timeout):
        calls.append(url)
        assert timeout == 30
        return io.BytesIO(
            json.dumps(
                {
                    "urls": [
                        {
                            "filename": "toolframe_web-0.1.0.tar.gz",
                            "packagetype": "sdist",
                            "url": "https://files.pythonhosted.org/toolframe_web-0.1.0.tar.gz",
                            "digests": {"sha256": "0" * 64},
                        }
                    ]
                }
            ).encode()
        )

    monkeypatch.setattr(validation.urllib.request, "urlopen", request)
    with pytest.raises(ValueError, match="metadata hash mismatch"):
        validation.prepare_sdist(root, "toolframe-web", pin)
    assert calls == ["https://pypi.org/pypi/toolframe-web/0.1.0/json"]
