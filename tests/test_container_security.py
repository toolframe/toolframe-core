"""Security publication policy fails closed and retains unfixed findings."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check-container-security.py"
SPEC = importlib.util.spec_from_file_location("container_security", SCRIPT)
security = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(security)


def report(*findings):
    return {
        "SchemaVersion": 2,
        "Metadata": {"OS": {"Family": "debian", "Name": "13.7"}},
        "Results": [
            {
                "Class": "os-pkgs",
                "Type": "debian",
                "Target": "test-image",
                "Packages": [{"Name": "libc6", "Version": "2.41"}],
                "Vulnerabilities": list(findings),
            }
        ],
    }


def finding(severity="HIGH", fixed=""):
    return {
        "VulnerabilityID": "TEST-ADVISORY",
        "PkgName": "test-package",
        "InstalledVersion": "1.0",
        "FixedVersion": fixed,
        "Severity": severity,
    }


@pytest.mark.parametrize("severity", ["CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"])
def test_every_available_fix_blocks_release(severity):
    findings, blockers = security.evaluate(report(finding(severity, "1.1")))
    assert blockers == findings and len(blockers) == 1


def test_critical_without_a_fix_blocks_release():
    assert len(security.evaluate(report(finding("CRITICAL")))[1]) == 1


def test_unfixed_findings_remain_visible_without_blocking():
    source = report(finding())
    findings, blockers = security.evaluate(source)
    assert len(findings) == 1 and not blockers
    rendered = security.summary(findings, blockers)
    assert "| HIGH | 1 |" in rendered and "**PASS**" in rendered


def test_valid_clean_report_passes():
    assert security.evaluate(report()) == ([], [])


@pytest.mark.parametrize(
    "source", [None, {}, {"SchemaVersion": 2}, {**report(), "Results": []}]
)
def test_missing_inventory_is_rejected(source):
    with pytest.raises(ValueError):
        security.evaluate(source)


def test_eol_os_is_rejected():
    source = report()
    source["Metadata"]["OS"]["EOSL"] = True
    with pytest.raises(ValueError, match="no longer supported"):
        security.evaluate(source)


def test_truncated_finding_is_rejected():
    with pytest.raises(ValueError, match="Incomplete"):
        security.evaluate(report({"VulnerabilityID": "TEST"}))


def test_cli_emits_failure_and_saves_evidence(tmp_path):
    source = tmp_path / "scan.json"
    output = tmp_path / "summary.md"
    source.write_text(json.dumps(report(finding("UNKNOWN", "1.1"))))
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(source), "--summary", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "TEST-ADVISORY" in output.read_text()


def test_cli_rejects_malformed_json(tmp_path):
    source = tmp_path / "scan.json"
    source.write_text('{"Results":')
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(source)], capture_output=True, check=False
    )
    assert result.returncode == 2
