import json
from pathlib import Path

import httpx

from toolframe_core import cli
from toolframe_core.config import ConfigFile


def install_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    client_type = cli.httpx.Client

    def make_client(**kwargs):
        return client_type(transport=transport, **kwargs)

    monkeypatch.setattr(cli.httpx, "Client", make_client)


def initialized_home(tmp_path):
    home = tmp_path / "app"
    ConfigFile(home).initialize(port=8765)
    return home


def test_json_flag_survives_before_and_after_nested_subcommands():
    cases = (
        ("--json", "status"),
        ("status", "--json"),
        ("--json", "plugin", "status", "demo"),
        ("plugin", "--json", "status", "demo"),
        ("plugin", "status", "demo", "--json"),
        ("--json", "migrate", "apply", "demo", "--plan-id", "p"),
        ("migrate", "--json", "apply", "demo", "--plan-id", "p"),
    )
    for arguments in cases:
        assert cli.parser().parse_args(arguments).json is True


def test_plan_wire_shape_has_no_force_and_supports_no_wait(
    tmp_path, monkeypatch, capsys
):
    home = initialized_home(tmp_path)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            202,
            json={"id": "operation-1", "status": "queued"},
            request=request,
        )

    install_client(monkeypatch, handler)
    assert (
        cli.main(
            [
                "--home",
                str(home),
                "plan",
                "--action",
                "reload",
                "--id",
                "demo",
                "--no-wait",
            ]
        )
        == 0
    )
    assert requests[0].url.path == "/_toolframe/v1/operations"
    assert json.loads(requests[0].content) == {
        "action": "plan",
        "params": {"action": "reload", "id": "demo"},
    }
    assert json.loads(capsys.readouterr().out)["status"] == "queued"


def test_plugin_update_normalizes_source_and_forwards_revision(tmp_path, monkeypatch):
    home = initialized_home(tmp_path)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            202,
            json={"id": "operation-1", "status": "queued"},
            request=request,
        )

    install_client(monkeypatch, handler)
    assert (
        cli.main(
            [
                "--home",
                str(home),
                "plugin",
                "update",
                "demo",
                "--source",
                "./plugin",
                "--expected-revision",
                "revision-1",
                "--no-wait",
            ]
        )
        == 0
    )
    assert json.loads(requests[0].content) == {
        "action": "update",
        "params": {
            "id": "demo",
            "source": str(Path("./plugin").resolve()),
            "expected_revision": "revision-1",
        },
    }


def test_status_check_and_operation_error_exit_codes(tmp_path, monkeypatch, capsys):
    home = initialized_home(tmp_path)

    def handler(request):
        if request.url.path.endswith("/status"):
            return httpx.Response(
                200,
                json={
                    "core": "ready",
                    "application": "degraded",
                    "configuration_error": None,
                    "plugins": [],
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "id": "operation-1",
                "status": "failed",
                "error": {"code": "stale_plan", "message": "old plan"},
            },
            request=request,
        )

    install_client(monkeypatch, handler)
    assert cli.main(["--home", str(home), "status", "--check"]) == 3
    capsys.readouterr()
    assert cli.main(["--home", str(home), "operation", "status", "operation-1"]) == 5
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
    assert cli._status_check({"ready": True, "degraded": True}) == 0
    assert cli._status_check({"ready": False, "degraded": True}) == 3


def test_http_error_uses_error_object_and_status_mapping(tmp_path, monkeypatch, capsys):
    home = initialized_home(tmp_path)

    def handler(request):
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "already_installed",
                    "message": "Plugin already exists",
                    "details": {"id": "demo"},
                }
            },
            request=request,
        )

    install_client(monkeypatch, handler)
    assert cli.main(["--home", str(home), "apply"]) == 5
    error = json.loads(capsys.readouterr().err)["error"]
    assert error == {
        "code": "already_installed",
        "message": "Plugin already exists",
        "details": {"id": "demo"},
    }


def test_export_writes_exclusively_or_emits_raw_toml(tmp_path, monkeypatch, capsys):
    home = initialized_home(tmp_path)
    toml = "[application]\nid='app'\n"

    def handler(request):
        return httpx.Response(
            200,
            json={"toml": toml, "revision": "revision-1"},
            request=request,
        )

    install_client(monkeypatch, handler)
    assert cli.main(["--home", str(home), "export"]) == 0
    assert capsys.readouterr().out == toml

    output = tmp_path / "composition.toml"
    assert cli.main(["--home", str(home), "export", "--output", str(output)]) == 0
    assert output.read_text() == toml
    assert cli.main(["--home", str(home), "export", "--output", str(output)]) == 2
    assert output.read_text() == toml
    assert "Refusing to overwrite" in capsys.readouterr().err


def test_invalid_toml_status_and_open_use_endpoint_file(tmp_path, monkeypatch, capsys):
    home = tmp_path / "invalid"
    home.mkdir()
    (home / "toolframe.toml").write_text("not = [valid")
    endpoint = home / ".toolframe"
    endpoint.mkdir()
    (endpoint / "endpoint.json").write_text(
        json.dumps({"host": "127.0.0.1", "port": 9999})
    )

    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "core": "ready",
                "application": "ready",
                "configuration_error": None,
                "plugins": [],
            },
            request=request,
        )

    install_client(monkeypatch, handler)
    assert cli.main(["--home", str(home), "status"]) == 0
    capsys.readouterr()
    assert requests[0].url == "http://127.0.0.1:9999/_toolframe/v1/status"

    assert cli.main(["--home", str(home), "open"]) == 0
    assert capsys.readouterr().out.startswith(
        "http://127.0.0.1:9999/_toolframe/session"
    )
