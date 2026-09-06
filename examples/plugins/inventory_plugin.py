"""A business service whose identity stays stable across implementations."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from fastapi_cordis import Plugin, ServiceKey
from toolframe_database_service import DATABASE, DatabaseService
from toolframe_migrations import Revision, Schema


@runtime_checkable
class Inventory(Protocol):
    def add_item(self, name: str) -> int: ...

    def list_names(self) -> list[str]: ...


INVENTORY = ServiceKey("example.inventory", Inventory)


@dataclass
class LocalInventory:
    database: DatabaseService

    def add_item(self, name: str) -> int:
        return self.database.insert("inventory_items", {"name": name})

    def list_names(self) -> list[str]:
        return self.database.query("SELECT name FROM inventory_items ORDER BY id")[
            "name"
        ].tolist()


PLUGIN = Plugin(
    "inventory.local",
    lambda context: LocalInventory(context.require(DATABASE)),
    provides=INVENTORY,
    requires=(DATABASE,),
)
HERE = Path(__file__).parent
SCHEMA = Schema(
    owner="inventory",
    target="1",
    revisions=(
        Revision(
            "1",
            sql={
                "sqlite": HERE / "sql/sqlite.sql",
                "postgresql": HERE / "sql/postgresql.sql",
            },
            tables=("inventory_items",),
        ),
    ),
)
