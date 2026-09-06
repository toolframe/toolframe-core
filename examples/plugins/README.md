# Business plugin with an owned schema

Run `python examples/plugins/run.py` from the Toolframe checkout using its Python
3.14 development environment. The script creates a disposable SQLite database,
starts the real Toolframe plugin manager, adds an item, and proves stopping and
re-enabling the service keeps the data. It cleans up its temporary directory.

`inventory_plugin.py` separates three identities:

- `example.inventory`: the typed service callers lease.
- `inventory.local`: this concrete service implementation.
- `inventory`: the stable schema owner for `inventory_items`.

In an installed application, make this example module importable (normally by
packaging it as a dependency in `app/pyproject.toml`) and declare:

```python
from inventory_plugin import PLUGIN, SCHEMA

PLUGINS = [PLUGIN]
SCHEMAS = {PLUGIN.name: SCHEMA}
DOMAIN_PLUGINS = {"entry_register": (PLUGIN.name,)}
```

Call `manager.lease(INVENTORY)` or `use_plugin(INVENTORY)` in business tools.
Callers use `add_item()` rather than reaching into the other plugin's SQL tables.
The host applies `SCHEMA` before setup. Both database SQL variants are provided;
the default example runner deliberately operates only on its temporary SQLite
file. Runtime and migration dependencies remain separate contracts.
