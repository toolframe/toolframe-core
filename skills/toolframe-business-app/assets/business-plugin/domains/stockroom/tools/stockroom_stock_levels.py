# /// script
# requires-python = ">=3.14,<3.15"
# dependencies = []
# ///
from domains.stockroom import STOCKROOM
from fastmcp.tools import tool
from toolframe_plugins import use_plugin


@tool
def stockroom_stock_levels() -> dict:
    """Report received stock totals for up to 100 items, ordered by item name."""
    with use_plugin(STOCKROOM) as stockroom:
        return {"items": stockroom.stock_levels(), "limit": 100}
