# Stockroom domain model

**Delivery** means an authorized receipt of one item and positive whole quantity.
**Delivery reference** identifies that external receipt, independently of an
internal database ID. **Stock level** is the sum of received quantities per item.

References are trimmed, required and at most 64 characters; item names are trimmed,
required and at most 80. A delivery quantity is an integer within the configured
limit (default 1,000). Retrying identical input under the same reference replays
the committed receipt; changed input for that reference is rejected. The receipt
and stock mutation commit together. This example has no dispatch/decrement or
correction capability, so the owner must define those intents before adding tools.

The `stockroom` domain owns `stockroom_deliveries`, its manifest, model, public
service contract, tools, widget and peer guide. `STOCKROOM` is the cross-domain
contract; consumers declare it in their plugin's `requires` and use `ctx.require`
during setup, or lease it for the complete call. Neither stop nor uninstall deletes
deliveries, receipts or migration history. SQLite/PostgreSQL targets are independent.
