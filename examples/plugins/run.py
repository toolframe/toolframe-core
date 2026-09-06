"""Run the business plugin example against a disposable SQLite database."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core"))

import anyio
from inventory_plugin import INVENTORY
from toolframe_database_config import DatabaseSettings
from toolframe_database_service import create_provider
from toolframe_db import Database
from toolframe_plugins import configure_plugins


async def exercise(root):
    app, domains = root / "app", root / "domains"
    app.mkdir()
    domains.mkdir()
    (app / "plugins.py").write_text(
        "from inventory_plugin import PLUGIN, SCHEMA\n"
        "PLUGINS = [PLUGIN]\n"
        "SCHEMAS = {PLUGIN.name: SCHEMA}\n"
    )
    provider = create_provider(DatabaseSettings(sqlite_path=root / "data.db"))
    manager = configure_plugins(
        app_dir=app,
        domains_dir=domains,
        database=Database(provider, ready=False),
        artifacts=object(),
    )
    try:
        async with manager.lifespan():
            with manager.lease(INVENTORY) as inventory:
                assert inventory.add_item("Pencil") > 0
                assert inventory.list_names() == ["Pencil"]
            await manager.disable("inventory.local")
            await manager.enable("inventory.local")
            with manager.lease(INVENTORY) as inventory:
                assert inventory.list_names() == ["Pencil"]
        assert provider._closed
    finally:
        provider.close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="toolframe-plugin-example-") as directory:
        anyio.run(exercise, Path(directory))
    print("Typed service, schema startup, and disable/re-enable preservation passed.")
