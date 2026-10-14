"""Gate a complete Trivy container report without hiding unfixed findings."""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def evaluate(report):
    """Return all findings and blockers; reject missing or incomplete OS scans."""
    if not isinstance(report, dict) or report.get("SchemaVersion") != 2:
        raise ValueError("Expected a Trivy schema 2 report")
    os_info = report.get("Metadata", {}).get("OS", {})
    if os_info.get("Family") != "debian" or not os_info.get("Name"):
        raise ValueError("The Debian OS inventory is missing")
    if os_info.get("EOSL"):
        raise ValueError("The image OS is no longer supported")
    results = report.get("Results")
    if not isinstance(results, list) or not any(
        isinstance(result, dict)
        and result.get("Class") == "os-pkgs"
        and result.get("Type") == "debian"
        and result.get("Packages")
        for result in results
    ):
        raise ValueError("A nonempty Debian package scan is required")
    findings, blockers = [], []
    for result in results:
        if not isinstance(result, dict):
            raise TypeError("Invalid scan result")
        vulnerabilities = result.get("Vulnerabilities", [])
        if not isinstance(vulnerabilities, list):
            raise TypeError("Invalid vulnerability list")
        for item in vulnerabilities:
            if not isinstance(item, dict) or not all(
                isinstance(item.get(field), str) and item[field]
                for field in (
                    "VulnerabilityID",
                    "PkgName",
                    "Severity",
                    "InstalledVersion",
                )
            ):
                raise ValueError("Incomplete vulnerability finding")
            if item["Severity"] not in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"):
                raise ValueError("Unrecognized vulnerability severity")
            fixed = item.get("FixedVersion", "")
            if not isinstance(fixed, str):
                raise TypeError("Invalid fixed version")
            finding = {**item, "Target": result.get("Target", "unknown")}
            findings.append(finding)
            if item["Severity"] == "CRITICAL" or fixed.strip():
                blockers.append(finding)
    return findings, blockers


def summary(findings, blockers):
    counts = Counter(item["Severity"] for item in findings)
    lines = [
        "## Container security scan",
        "",
        (
            f"Result: **{'FAIL' if blockers else 'PASS'}**. "
            "Any Critical finding or finding with a published fix blocks publication."
        ),
        "",
        "Counts are package/advisory occurrences, not unique or proven exploitable CVEs.",
        "",
        "| Severity | Findings |",
        "| --- | ---: |",
        *(
            f"| {severity} | {counts[severity]} |"
            for severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")
        ),
        "",
        (
            f"Blocking findings: **{len(blockers)}**. "
            "The unfiltered JSON and CycloneDX inventory are saved as workflow artifacts."
        ),
        "Passing this policy does not establish that an image is free of vulnerabilities.",
    ]
    if blockers:
        lines.extend(
            [
                "",
                "| Advisory | Package | Installed | Fixed |",
                "| --- | --- | --- | --- |",
            ]
        )
        for item in blockers:
            values = (
                item["VulnerabilityID"],
                item["PkgName"],
                item["InstalledVersion"],
                item.get("FixedVersion") or "Unavailable",
            )
            lines.append(
                "| "
                + " | ".join(
                    value.replace("|", "\\|").replace("\n", " ") for value in values
                )
                + " |"
            )
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()
    try:
        report = json.loads(args.report.read_text())
        findings, blockers = evaluate(report)
        rendered = summary(findings, blockers)
    except (OSError, ValueError, TypeError, AttributeError) as error:
        print(f"Security scan rejected: {error}", file=sys.stderr)
        return 2
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(rendered)
    print(rendered)
    return int(bool(blockers))


if __name__ == "__main__":
    raise SystemExit(main())
