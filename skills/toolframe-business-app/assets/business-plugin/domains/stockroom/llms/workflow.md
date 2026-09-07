# Receive a delivery

1. Connect to the same server's `/mcp` using Streamable HTTP and list its tools.
2. Call `stockroom_receive_delivery(reference, item, quantity)` for a delivery
   the owner authorized. Use its stable reference and a positive whole quantity.
3. If retrying the same delivery, reuse all three values exactly. A successful
   replay returns `replayed: true` and does not add stock twice. A reused reference
   with changed contents is rejected; ask the owner to resolve that discrepancy.
4. Call `stockroom_stock_levels()` to review up to 100 item totals.

The human form performs the same delivery operation. If a plugin is unavailable,
preserve its error and recovery hint for the operator. Ask the master development
agent for unsupported business behavior; these tools do not alter schema or source.
