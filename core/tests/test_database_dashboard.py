"""Run the actual Streamlit business form against both database providers."""

import logging
import shutil
from pathlib import Path

import anyio
from streamlit.testing.v1 import AppTest
from test_database_providers import provider as provider  # noqa: PLC0414
from toolframe_artifacts import configure_artifact_store
from toolframe_dashboard import _configure, preflight_app
from toolframe_db import Database
from toolframe_plugins import configure_plugins


def test_streamlit_form_persists_and_domain_disable_preserves_rows(provider, tmp_path):
    repository = Path(__file__).resolve().parents[2]
    for name in ("app", "domains"):
        shutil.copytree(
            repository / name,
            tmp_path / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    app, domains = tmp_path / "app", tmp_path / "domains"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifacts = configure_artifact_store(workspace)
    database = Database(provider, ready=False)
    manager = configure_plugins(
        app_dir=app, domains_dir=domains, database=database, artifacts=artifacts
    )
    _configure(
        database=database,
        app_dir=app,
        domains_dir=domains,
        workspace=workspace,
        logger=logging.getLogger("test.streamlit"),
        runtime={"database": provider.public_info()},
        artifacts=artifacts,
        plugins=manager,
    )
    preflight_app(app, domains)
    ui = AppTest.from_file(app / "dashboard.py", default_timeout=15)

    def add_entry():
        ui.run()
        assert not ui.exception
        ui.text_input[0].set_value("Notebook")
        ui.number_input[0].set_value(12.5)
        next(
            button for button in ui.button if button.label == "Add entry"
        ).click().run()
        assert not ui.exception and not ui.error
        assert any("added" in item.value for item in ui.success)
        assert ui.dataframe[0].value["label"].tolist() == ["Notebook"]
        ui.text_input[0].set_value("   ")
        next(
            button for button in ui.button if button.label == "Add entry"
        ).click().run()
        assert not ui.exception
        assert any("entry_register_invalid_input" in item.value for item in ui.error)
        assert database.query("SELECT label,value FROM entry_register_entries").iloc[
            0
        ].tolist() == ["Notebook", 12.5]

    async def exercise():
        async with manager.lifespan():
            await anyio.to_thread.run_sync(add_entry)
            await manager.disable("domain.entry_register")
            await anyio.to_thread.run_sync(ui.run)
            assert not ui.exception and not ui.text_input
            assert any("domain.entry_register" in item.value for item in ui.info)
            await manager.enable("domain.entry_register")
            await anyio.to_thread.run_sync(ui.run)
            assert not ui.exception
            assert ui.dataframe[0].value["label"].tolist() == ["Notebook"]

    anyio.run(exercise)
