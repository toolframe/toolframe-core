"""Release validation accepts the architecture names used by launcher CI."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location(
    "release_acceptance_verifier", ROOT / "scripts/verify-release.py"
)
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)
helper = sys.modules["verification_workspace"]


@pytest.mark.parametrize("architecture", ["arm64", "x86_64", "x86_64-rosetta"])
def test_missing_binary_preserves_failure_evidence_for_each_architecture(
    tmp_path, monkeypatch, architecture
):
    monkeypatch.setattr(helper, "ROOT", tmp_path)
    monkeypatch.setattr(helper, "idle", lambda path: True)
    monkeypatch.delenv("TOOLFRAME_KEEP_TEST_WORKSPACE", raising=False)
    report = tmp_path / "evidence/release.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify-release.py",
            "--binary",
            str(tmp_path / "missing-binary"),
            "--architecture",
            architecture,
            "--output",
            str(report),
        ],
    )
    with pytest.raises(FileNotFoundError):
        verifier.main()
    (sidecar,) = report.parent.glob("release.json.logs/*/workspace.json")
    summary = json.loads(sidecar.read_text())
    assert summary["status"] == "failed"
    assert "FileNotFoundError" in summary["error"]
    assert Path(summary["path"]).is_dir()
