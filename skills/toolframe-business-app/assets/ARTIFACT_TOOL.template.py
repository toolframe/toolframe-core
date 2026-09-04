# /// script
# requires-python = ">=3.14,<3.15"
# dependencies = ["fastmcp==4.0.0"]
# ///
"""Copy this private example to ``export_report.py`` to enable the tool."""

from datetime import UTC, datetime

from fastmcp.tools import tool
from toolframe_artifacts import get_artifact_store


@tool
def export_report() -> dict[str, object]:
    """Create a small CSV report and return a managed artifact reference."""

    store = get_artifact_store()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    artifact = store.write_text(
        f"reports/example-{timestamp}.csv",
        "category,total\nexample,42\n",
        mime_type="text/csv",
    )
    return {
        "artifact": artifact.to_reference(),
        "nextStep": "Read the artifact through toolframe_artifacts instead of returning its bytes in MCP.",
    }
