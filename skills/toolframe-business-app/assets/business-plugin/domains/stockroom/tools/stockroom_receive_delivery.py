# /// script
# requires-python = ">=3.14,<3.15"
# dependencies = []
# ///
from domains.stockroom import STOCKROOM
from domains.stockroom.model.service import DeliveryError
from fastmcp.tools import tool
from toolframe_database import DashboardDatabaseError
from toolframe_idempotency import IdempotencyError
from toolframe_plugins import use_plugin
from toolframe_runtime import ToolframeError


@tool
def stockroom_receive_delivery(reference: str, item: str, quantity: int) -> dict:
    """Record one delivery; reuse its reference and unchanged contents for a safe retry."""
    try:
        with use_plugin(STOCKROOM) as stockroom:
            return stockroom.receive_delivery(reference, item, quantity)
    except DeliveryError as error:
        raise ToolframeError(
            "stockroom_invalid_delivery", str(error), retryable=False
        ) from error
    except (DashboardDatabaseError, IdempotencyError) as error:
        raise ToolframeError(
            error.code, error.message, retryable=error.retryable
        ) from error
