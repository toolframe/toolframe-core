"""One operator surface over the same persistent management operations."""

import argparse
import json
import sys
import time
import urllib.parse
from pathlib import Path

import httpx
import uvicorn
from toolframe_sdk import PluginError

from .config import ConfigFile, management_token


def parser():
    result = argparse.ArgumentParser(prog="toolframe")
    result.add_argument("--home", type=Path, default=Path.cwd())
    commands = result.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init")
    initialize.add_argument("--name", default="Toolframe")
    initialize.add_argument("--port", type=int, default=9876)
    commands.add_parser("serve")
    commands.add_parser("status")
    commands.add_parser("open")
    apply = commands.add_parser("apply")
    apply.add_argument("--no-wait", action="store_true")
    plugin = commands.add_parser("plugin").add_subparsers(dest="action", required=True)
    add = plugin.add_parser("add")
    add.add_argument("source")
    add.add_argument("--id")
    add.add_argument("--enable", action="store_true")
    add.add_argument("--no-wait", action="store_true")
    for name in (
        "validate",
        "reload",
        "enable",
        "disable",
        "remove",
        "stop",
        "status",
        "logs",
    ):
        command = plugin.add_parser(name)
        command.add_argument("id", nargs="?" if name == "status" else None)
        command.add_argument("--no-wait", action="store_true")
        if name == "validate":
            command.add_argument("--runtime", action="store_true")
        if name == "stop":
            command.add_argument("--force", action="store_true")
    migration = commands.add_parser("migrate").add_subparsers(
        dest="action", required=True
    )
    for name in ("plan", "apply"):
        command = migration.add_parser(name)
        command.add_argument("id")
        if name == "apply":
            command.add_argument("--plan-id", required=True)
    operation = commands.add_parser("operation").add_subparsers(
        dest="action", required=True
    )
    for name in ("status", "cancel"):
        operation.add_parser(name).add_argument("id")
    return result


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        return run(arguments)
    except (PluginError, httpx.HTTPError, OSError, ValueError) as error:
        value = (
            error.as_dict()
            if isinstance(error, PluginError)
            else {"code": "command_failed", "message": str(error)}
        )
        print(json.dumps({"error": value}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from None


def run(arguments):
    config = ConfigFile(arguments.home)
    if arguments.command == "init":
        config.initialize(name=arguments.name, port=arguments.port)
        print(f"Initialized {config.path}. Run toolframe --home {config.home} serve")
        return
    composition, _ = config.read()
    host = composition.application.host
    authority = f"[{host}]" if ":" in host else host
    base = f"http://{authority}:{composition.application.port}"
    token = management_token(config.home)
    if arguments.command == "open":
        print(f"{base}/_toolframe/session?token={urllib.parse.quote(token)}")
        return
    if arguments.command == "serve":
        from .api import create_app
        from .host import Host

        control = Host(config.home)
        print(
            f"Toolframe: {base} · management token: {config.home / '.toolframe/management.token'}",
            flush=True,
        )
        uvicorn.run(
            create_app(control),
            host=host,
            port=composition.application.port,
            log_level="info",
        )
        return
    with httpx.Client(
        base_url=base,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
        trust_env=False,
    ) as client:
        if arguments.command == "status" or (
            arguments.command == "plugin" and arguments.action == "status"
        ):
            response = client.get("/_toolframe/status")
            response.raise_for_status()
            result = response.json()
            if getattr(arguments, "id", None):
                result = next(
                    (item for item in result["plugins"] if item["id"] == arguments.id),
                    {},
                )
        elif arguments.command == "plugin" and arguments.action == "logs":
            response = client.get(f"/_toolframe/plugins/{arguments.id}/logs")
            response.raise_for_status()
            result = response.json()
        elif arguments.command == "operation":
            path = f"/_toolframe/operations/{arguments.id}"
            response = (
                client.post(path + "/cancel")
                if arguments.action == "cancel"
                else client.get(path)
            )
            response.raise_for_status()
            result = response.json()
        else:
            action = (
                "apply"
                if arguments.command == "apply"
                else f"migrate-{arguments.action}"
                if arguments.command == "migrate"
                else arguments.action
            )
            params = {
                key: value
                for key, value in vars(arguments).items()
                if key not in ("home", "command", "action", "no_wait")
                and value is not None
            }
            if "source" in params and not params["source"].startswith(
                ("git+", "pypi:", "https://", "path:", "wheel:")
            ):
                params["source"] = str(Path(params["source"]).resolve())
            response = client.post(
                "/_toolframe/operations", json={"action": action, "params": params}
            )
            response.raise_for_status()
            result = response.json()
            if not getattr(arguments, "no_wait", False):
                while result["status"] in ("queued", "running", "cancelling"):
                    time.sleep(0.2)
                    response = client.get(f"/_toolframe/operations/{result['id']}")
                    response.raise_for_status()
                    result = response.json()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if isinstance(result, dict) and result.get("status") in (
            "failed",
            "failed_recovered",
            "cancelled",
            "interrupted",
        ):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
