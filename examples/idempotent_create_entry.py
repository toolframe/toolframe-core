# /// script
# requires-python = ">=3.14,<3.15"
# dependencies = [
#     "fastmcp==4.0.0",
# ]
# ///

"""Reference MCP tool using explicit durable idempotency."""

from fastmcp.tools import tool
from toolframe_idempotency import IdempotencyTransaction, run_once


@tool
def idempotent_create_entry(
    idempotency_key: str, label: str, value: float
) -> dict:
    """Create one example entry and safely replay it after a transport retry."""

    request = {"label": label, "value": value}

    def create(transaction: IdempotencyTransaction) -> dict:
        cursor = transaction.execute(
            "INSERT INTO dashboard_example_entries(label, value) VALUES (?, ?)",
            (label, value),
        )
        return {"id": cursor.lastrowid, **request}

    outcome = run_once(
        "dashboard-example.create-entry", idempotency_key, request, create
    )
    return {"entry": outcome.value, "replayed": outcome.replayed}
