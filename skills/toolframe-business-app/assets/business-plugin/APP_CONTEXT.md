# Stockroom app context

The owner authorizes received deliveries. A human form and trusted peer MCP
command record each delivery reference once; both read the resulting stock totals.
This is a local example with no authentication, dispatch workflow or durable worker.

`domains/stockroom/model/service.py` owns validation and transactional delivery
recording. The public `STOCKROOM` key identifies the `Stockroom` contract;
`stockroom.local` implements it and requires the host `DATABASE` service.
`app/plugins.py` registers it and binds `domain.stockroom` through `DOMAIN_PLUGINS`.
The schema owner is `stockroom`, target `0000_deliveries`, discovered from the
domain manifest. MCP and UI lease the business service; stopping its implementation
blocks the domain and preserves deliveries. There is no separate empty domain plugin.

Use `check`, start the server, record/replay a delivery, inspect totals, then
stop/re-enable `stockroom.local` at `/info` and verify data remains. Install new
package dependencies through `app/pyproject.toml` and restart before registration.
This local plugin needs no additional package. Its contract does not authorize
another domain to import model/storage internals. Reconcile this file and
`DOMAIN_MODEL.md` when adapting the example to the owner's actual business.
