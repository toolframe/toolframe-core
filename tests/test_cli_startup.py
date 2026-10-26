"""Application selection, startup diagnostics and recovery at the public CLI."""

import asyncio
import json

import pytest

from toolframe_core import __version__, cli
from toolframe_core.config import ConfigFile
from toolframe_core.host import Host


@pytest.mark.parametrize("nested_app", [False, True])
def test_uninitialized_cwd_is_rejected_without_creating_runtime(
    tmp_path, monkeypatch, capsys, nested_app
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TOOLFRAME_CORE_STATE_BACKEND", raising=False)
    if nested_app:
        ConfigFile(tmp_path / ".local/app").initialize(name="Another application")
    monkeypatch.setattr(cli, "_serve", lambda *args, **kwargs: 0)

    assert cli.main(["serve"]) == 2
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "application_not_initialized"
    assert error["details"]["home"] == str(tmp_path)
    assert error["details"]["configuration"] == str(tmp_path / "toolframe.toml")
    assert "toolframe init" in error["message"]
    assert "--home" in error["message"]
    assert not (tmp_path / ".toolframe").exists()
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / ".agents").exists()


def test_uninitialized_explicit_home_is_not_created(tmp_path, monkeypatch, capsys):
    home = tmp_path / "chosen app"
    monkeypatch.delenv("TOOLFRAME_CORE_STATE_BACKEND", raising=False)
    monkeypatch.setattr(cli, "_serve", lambda *args, **kwargs: 0)
    assert cli.main(["--home", str(home), "serve"]) == 2
    assert "application_not_initialized" in capsys.readouterr().err
    assert not home.exists()


@pytest.mark.parametrize("explicit_home", [False, True])
def test_startup_identifies_selected_home_and_configuration(
    tmp_path, monkeypatch, capsys, explicit_home
):
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "chosen app 中文" if explicit_home else tmp_path
    config = ConfigFile(home)
    application = config.initialize(name="Selected application", port=8765).application
    original = config.path.read_bytes()
    observed = []

    async def serve(server):
        async with server.config.app.router.lifespan_context(server.config.app):
            observed.append(server.config.port)

    monkeypatch.setattr(cli.uvicorn.Server, "serve", serve)
    arguments = ["--home", str(home), "serve"] if explicit_home else ["serve"]
    assert cli.main(arguments) == 0
    assert observed == [8765]
    output = capsys.readouterr()
    assert f"Application home: {home}" in output.out
    assert f"Configuration: {config.path}" in output.out
    assert application.id in output.out and application.name in output.out
    assert f"Core {__version__}" in output.out
    assert "status --check" in output.out
    assert "Application not ready" not in output.err
    assert config.path.read_bytes() == original
    if explicit_home:
        assert not (tmp_path / ".toolframe").exists()
        assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("missing", [False, True])
def test_existing_application_keeps_management_recovery(
    tmp_path, monkeypatch, capsys, missing
):
    config = ConfigFile(tmp_path)
    application = config.initialize(name="Retained application", port=8765).application

    async def prepare():
        host = await Host.create(tmp_path)
        try:
            await host.start()
        finally:
            await host.close()

    asyncio.run(prepare())
    if missing:
        config.path.unlink()
    else:
        config.path.write_text("invalid = [")
    observed = []

    async def serve(server):
        async with server.config.app.router.lifespan_context(server.config.app):
            observed.append(server.config.port)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.uvicorn.Server, "serve", serve)
    assert cli.main(["serve"]) == 0
    output = capsys.readouterr()
    assert observed == [8765]
    assert application.id in output.out
    assert "Application not ready" in output.err
    assert "management recovery" in output.err
    assert (tmp_path / ".toolframe/endpoint.json").is_file()
    assert config.path.exists() is not missing


def test_explicit_remote_journal_can_recover_without_local_config(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TOOLFRAME_CORE_STATE_BACKEND", "postgresql")
    monkeypatch.setenv("TOOLFRAME_CORE_DATABASE_URL", "postgresql://example/core")
    monkeypatch.setenv("TOOLFRAME_CORE_APPLICATION_ID", "remote-app")
    monkeypatch.setenv("TOOLFRAME_CORE_NODE_ID", "test-node")
    started = []
    monkeypatch.setattr(
        cli, "_serve", lambda config, **kwargs: started.append(config.home) or 0
    )
    assert cli.main(["--home", str(tmp_path), "serve"]) == 0
    assert started == [tmp_path]
    assert not (tmp_path / "data").exists()
