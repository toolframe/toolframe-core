"""One operator surface over the same persistent management operations."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.parse
from pathlib import Path

import httpx
import uvicorn
from toolframe_sdk import PluginError

from . import __version__
from .config import Application, ConfigFile, management_token
from .network import bind_host, client_host, http_url

_SOURCE_PREFIXES = ("git+", "pypi:", "https://", "path:", "wheel:")
_PENDING_OPERATION_STATES = ("queued", "running", "cancelling")


def _add_json_option(command):
    # Every parser owns the option so it works before or after any subcommand.
    # Suppressed defaults keep a value supplied by a parent parser intact.
    command.add_argument(
        "--json", dest="json", action="store_true", default=argparse.SUPPRESS
    )


def _add_no_wait(command):
    command.add_argument("--no-wait", action="store_true")


def _add_expected_revision(command):
    command.add_argument("--expected-revision")


def parser():
    result = argparse.ArgumentParser(
        prog="toolframe",
        epilog=(
            "Built launcher options: --offline (cached assets only), "
            "--wheelhouse PATH (local wheels), --uv-path PATH (uv executable)."
        ),
    )
    result.add_argument(
        "--version", "-V", action="version", version=f"toolframe {__version__}"
    )
    result.add_argument("--home", type=Path, default=Path.cwd())
    _add_json_option(result)
    commands = result.add_subparsers(dest="command", required=True)

    initialize = commands.add_parser("init")
    _add_json_option(initialize)
    initialize.add_argument("--name")
    initialize.add_argument("--template")
    initialize.add_argument(
        "--host",
        type=bind_host,
        help="Save the listen address (IPv4/IPv6 or localhost); default 127.0.0.1 for an empty app",
    )
    initialize.add_argument("--port", type=int, default=9876)

    skill = commands.add_parser("skill", help="Install bundled agent guidance locally")
    _add_json_option(skill)
    skill_commands = skill.add_subparsers(dest="action", required=True)
    skill_install = skill_commands.add_parser("install")
    _add_json_option(skill_install)
    skill_install.add_argument(
        "--output",
        type=Path,
        help="Exact destination directory; defaults to APP/.agents/skills/toolframe-plugin-design under --home",
    )
    skill_install.add_argument(
        "--overwrite",
        action="store_true",
        help="Confirm replacement of customized content; saves a backup ZIP",
    )

    serve = commands.add_parser("serve")
    _add_json_option(serve)
    serve.add_argument(
        "--host",
        type=bind_host,
        help="Override the listen address for this run, e.g. 0.0.0.0 for LAN access",
    )

    status = commands.add_parser("status")
    _add_json_option(status)
    status.add_argument("--check", action="store_true")

    open_command = commands.add_parser("open")
    _add_json_option(open_command)

    plan = commands.add_parser("plan")
    _add_json_option(plan)
    plan.add_argument(
        "--action",
        choices=("apply", "reload", "enable", "disable", "remove", "stop"),
        default="apply",
    )
    plan.add_argument("--id")
    _add_no_wait(plan)

    export = commands.add_parser("export")
    _add_json_option(export)
    export.add_argument("--output", type=Path)

    apply = commands.add_parser("apply")
    _add_json_option(apply)
    apply.add_argument("--plan-id")
    _add_expected_revision(apply)
    _add_no_wait(apply)

    plugin = commands.add_parser("plugin")
    _add_json_option(plugin)
    plugin_commands = plugin.add_subparsers(dest="action", required=True)

    new = plugin_commands.add_parser(
        "new", help="Generate a new plugin's source locally"
    )
    _add_json_option(new)
    new.add_argument("id")
    new.add_argument(
        "--output",
        type=Path,
        help="Exact destination directory; defaults to APP/plugins/PLUGIN-ID under --home",
    )

    add = plugin_commands.add_parser("add")
    _add_json_option(add)
    add.add_argument("source")
    add.add_argument("--id")
    add.add_argument("--enable", action="store_true")
    _add_expected_revision(add)
    _add_no_wait(add)

    plugin_action_names = (
        "validate",
        "update",
        "reload",
        "enable",
        "disable",
        "remove",
        "stop",
        "status",
        "logs",
    )
    for name in plugin_action_names:
        command = plugin_commands.add_parser(name)
        _add_json_option(command)
        command.add_argument("id", nargs="?" if name == "status" else None)
        if name == "validate":
            command.add_argument("--runtime", action="store_true")
        if name == "update":
            command.add_argument("--source")
        if name in ("reload", "enable", "disable", "remove", "stop"):
            command.add_argument("--plan-id")
        if name == "stop":
            command.add_argument("--force", action="store_true")
        if name == "status":
            command.add_argument("--check", action="store_true")
        if name not in ("status", "logs"):
            _add_expected_revision(command)
            _add_no_wait(command)

    migration = commands.add_parser("migrate")
    _add_json_option(migration)
    migration_commands = migration.add_subparsers(dest="action", required=True)
    migration_plan = migration_commands.add_parser("plan")
    _add_json_option(migration_plan)
    migration_plan.add_argument("id")
    _add_no_wait(migration_plan)
    migration_apply = migration_commands.add_parser("apply")
    _add_json_option(migration_apply)
    migration_apply.add_argument("id")
    migration_apply.add_argument("--plan-id", required=True)
    _add_expected_revision(migration_apply)
    _add_no_wait(migration_apply)

    operation = commands.add_parser("operation")
    _add_json_option(operation)
    operation_commands = operation.add_subparsers(dest="action", required=True)
    for name in ("status", "cancel"):
        command = operation_commands.add_parser(name)
        _add_json_option(command)
        command.add_argument("id")

    return result


def _normalize_source(source):
    if source and not source.startswith(_SOURCE_PREFIXES):
        return str(Path(source).resolve())
    return source


def _read_endpoint_file(config):
    fallback = ("127.0.0.1", 9876)
    try:
        value = json.loads((config.home / ".toolframe" / "endpoint.json").read_text())
        host = value["host"]
        port = int(value["port"])
        endpoint = Application(id="endpoint", host=host, port=port)
        return endpoint.host, endpoint.port
    except OSError, TypeError, ValueError, KeyError, json.JSONDecodeError:
        return fallback


def _endpoint(config):
    if (config.home / ".toolframe/endpoint.json").exists():
        return _read_endpoint_file(config)
    try:
        composition, _ = config.read()
    except PluginError:
        return _read_endpoint_file(config)
    application = composition.application
    return application.host, application.port


def _base_url(config):
    host, port = _endpoint(config)
    return http_url(client_host(host), port)


def _emit_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _error_code(error):
    if isinstance(error, PluginError):
        return error.code
    if isinstance(error, httpx.HTTPStatusError):
        response = error.response
        if response.status_code == 409:
            return "idempotency_conflict"
        if response.status_code == 422:
            return "malformed_input"
        if response.status_code == 503:
            return "runtime"
        payload = _http_error_payload(response)
        return payload.get("code", "http_error")
    if isinstance(error, ValueError):
        return "malformed_input"
    return "runtime"


def _exit_code(code):
    if code in {
        "invalid_config",
        "configuration_invalid",
        "configuration_exists",
        "config_invalid",
        "config_missing",
        "malformed_input",
        "invalid_input",
        "invalid_request",
    }:
        return 2
    if code in {
        "validation",
        "eligibility",
        "migration_required",
        "dependency",
        "dependency_unavailable",
        "storage_unavailable",
        "schema",
        "store_not_declared",
        "plugin_missing",
        "source_invalid",
        "identity_mismatch",
        "package_command_failed",
        "provider_conflict",
        "service_ambiguous",
        "resource_target_invalid",
    } or code.startswith(("validation_", "eligibility_", "dependency_", "schema_")):
        return 3
    if code in {
        "runtime",
        "cleanup",
        "forward_repair",
        "failed_recovered",
        "interrupted",
        "operation_failed",
    } or code.startswith(("runtime_", "cleanup_", "forward_repair_")):
        return 4
    if code in {
        "stale_plan",
        "stale_revision",
        "idempotency_conflict",
        "already_installed",
        "skill_conflict",
        "scaffold_conflict",
    }:
        return 5
    if code == "cancelled":
        return 130
    return 4


def _http_error_payload(response):
    try:
        payload = response.json()
    except TypeError, ValueError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        return payload["error"]
    message = getattr(response, "text", "") or f"HTTP {response.status_code}"
    return {"code": "http_error", "message": message, "details": {}}


def _failure_payload(error):
    if isinstance(error, PluginError):
        return error.as_dict()
    if isinstance(error, httpx.HTTPStatusError):
        return _http_error_payload(error.response)
    if isinstance(error, httpx.HTTPError):
        return {"code": "connection_error", "message": str(error), "details": {}}
    if isinstance(error, ValueError):
        return {"code": "malformed_input", "message": str(error), "details": {}}
    if isinstance(error, OSError):
        return {"code": "io_error", "message": str(error), "details": {}}
    return {"code": "runtime", "message": str(error), "details": {}}


def _response_json(response):
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as error:
        raise ValueError("Management API returned malformed JSON") from error


def _wait_for_operation(client, result):
    while (
        isinstance(result, dict) and result.get("status") in _PENDING_OPERATION_STATES
    ):
        time.sleep(0.2)
        result = _response_json(client.get(f"/_toolframe/v1/operations/{result['id']}"))
    return result


def _operation_exit_code(result, *, no_wait=False):
    if not isinstance(result, dict):
        return 4
    status = result.get("status")
    if status == "succeeded":
        return 0
    if no_wait and status in _PENDING_OPERATION_STATES:
        return 0
    if status == "cancelled":
        return 130
    if status in ("failed_recovered", "interrupted"):
        return 4
    if status == "failed":
        error = result.get("error") or {}
        return _exit_code(error.get("code", "runtime"))
    return 4


def _status_check(result, *, plugin=False):
    if plugin:
        rows = result if isinstance(result, list) else [result]
        return (
            0
            if rows
            and all(
                bool(row.get("active"))
                and isinstance(row.get("runtime"), dict)
                and row["runtime"].get("state") == "active"
                and row.get("health_fresh") is True
                for row in rows
            )
            else 3
        )
    if not isinstance(result, dict):
        return 3
    if result.get("configuration_error"):
        return 3
    if "ready" in result:
        return 0 if result["ready"] is True else 3
    if result.get("application") in ("not_ready", "degraded"):
        return 3
    if result.get("application") == "ready":
        return 0
    return 3


def _serve(config, *, listen_host=None):
    # Host owns the invalid-config fallback and is the only host constructed by
    # the CLI. Its application is therefore the source of the listen address.
    from .api import create_app
    from .host import Host

    control = Host(config.home, listen_host=listen_host)
    application = control.composition.application
    host = control.listen_host
    authority = f"[{host}]" if ":" in host else host
    print(
        f"Toolframe: {http_url(client_host(host), application.port)} "
        f"· listening on {authority}:{application.port} · management token: "
        f"{config.home / '.toolframe/management.token'}",
        flush=True,
    )
    uvicorn.run(
        create_app(control),
        host=host,
        port=application.port,
        log_level="info",
        access_log=False,
        proxy_headers=False,
    )
    return 0


def _operation_params(arguments, *, exclude=()):
    excluded = {
        "home",
        "command",
        "action",
        "no_wait",
        "json",
        "check",
        *exclude,
    }
    params = {
        key: value
        for key, value in vars(arguments).items()
        if key not in excluded and value is not None
    }
    if "source" in params:
        params["source"] = _normalize_source(params["source"])
    return params


def _install_skill(arguments, home, *, upgrade_only=False):
    from .skills import install_skill

    def confirm(result):
        print(
            f"Skill at {result['path']} has local changes or no installation record. "
            f"Replace it with bundled version {result['bundled_version']} and save a backup? [y/N] ",
            end="",
            file=sys.stderr,
            flush=True,
        )
        return sys.stdin.readline().strip().lower() in {"y", "yes"}

    return install_skill(
        home,
        output=getattr(arguments, "output", None),
        overwrite=getattr(arguments, "overwrite", False),
        confirm=confirm
        if sys.stdin.isatty() and not getattr(arguments, "json", False)
        else None,
        upgrade_only=upgrade_only,
    )


def run(arguments):
    config = ConfigFile(arguments.home)
    if arguments.command == "plugin" and arguments.action == "new":
        from .scaffold import new_plugin

        result = new_plugin(config.home, arguments.id, output=arguments.output)
        if getattr(arguments, "json", False):
            _emit_json(result)
        else:
            print(f"Created plugin {result['plugin_id']} at {result['path']}")
        return 0
    if arguments.command == "skill":
        from .skills import installation_message

        result = _install_skill(arguments, config.home)
        if getattr(arguments, "json", False):
            _emit_json(result)
        else:
            print(installation_message(result))
        return (
            5
            if result["confirmation_required"]
            or result.get("reason") == "non_regular_destination"
            else 0
        )
    if arguments.command == "init":
        if arguments.template:
            from .templates import initialize_template

            asyncio.run(
                initialize_template(
                    config,
                    arguments.template,
                    name=arguments.name,
                    host=arguments.host,
                    port=arguments.port,
                )
            )
        else:
            config.initialize(
                name=arguments.name or "Toolframe",
                host=arguments.host or "127.0.0.1",
                port=arguments.port,
            )
        from .skills import installation_message

        skill = _install_skill(arguments, config.home)
        if getattr(arguments, "json", False):
            _emit_json({"configuration": str(config.path), "skill": skill})
        else:
            print(
                f"Initialized {config.path}. Run toolframe --home {config.home} serve"
            )
            print(installation_message(skill))
        return 0
    if arguments.command == "serve":
        from .skills import installation_message

        try:
            skill = _install_skill(arguments, config.home, upgrade_only=True)
            if skill["status"] != "unchanged":
                print(installation_message(skill), file=sys.stderr)
        except (OSError, ValueError, PluginError) as error:
            # Authoring guidance must not prevent management and recovery startup.
            print(
                f"Skill refresh skipped: {error}. Use skill install to retry.",
                file=sys.stderr,
            )
        return _serve(config, listen_host=arguments.host)

    base = _base_url(config)
    token = management_token(config.home)
    if arguments.command == "open":
        print(f"{base}/_toolframe/session?token={urllib.parse.quote(token)}")
        return 0

    with httpx.Client(
        base_url=base,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
        trust_env=False,
    ) as client:
        if arguments.command == "export":
            payload = _response_json(client.get("/_toolframe/v1/composition"))
            if not isinstance(payload, dict) or not isinstance(
                payload.get("toml"), str
            ):
                raise ValueError("Composition response did not contain TOML")
            text = payload["toml"]
            if arguments.output:
                try:
                    with arguments.output.open("x", encoding="utf-8") as handle:
                        handle.write(text)
                except OSError as error:
                    raise ValueError(
                        f"Refusing to overwrite or create output {arguments.output}"
                    ) from error
            else:
                sys.stdout.write(text)
            return 0

        if arguments.command == "status" or (
            arguments.command == "plugin" and arguments.action == "status"
        ):
            result = _response_json(client.get("/_toolframe/v1/status"))
            plugin_status = arguments.command == "plugin"
            if plugin_status and getattr(arguments, "id", None):
                result = next(
                    (
                        item
                        for item in result.get("plugins", [])
                        if item["id"] == arguments.id
                    ),
                    {},
                )
            _emit_json(result)
            if getattr(arguments, "check", False):
                if plugin_status and not getattr(arguments, "id", None):
                    result = result.get("plugins", [])
                return _status_check(result, plugin=plugin_status)
            return 0

        if arguments.command == "plugin" and arguments.action == "logs":
            result = _response_json(
                client.get(f"/_toolframe/v1/plugins/{arguments.id}/logs")
            )
            _emit_json(result)
            return 0

        if arguments.command == "operation":
            path = f"/_toolframe/v1/operations/{arguments.id}"
            if arguments.action == "cancel":
                result = _response_json(client.post(path + "/cancel"))
                _emit_json(result)
                return 0
            result = _response_json(client.get(path))
            _emit_json(result)
            if (
                isinstance(result, dict)
                and result.get("status") in _PENDING_OPERATION_STATES
            ):
                return 0
            return _operation_exit_code(result)

        if arguments.command == "plan":
            params = {"action": arguments.action}
            if arguments.id is not None:
                params["id"] = arguments.id
            action = "plan"
        elif arguments.command == "apply":
            params = _operation_params(arguments)
            action = "apply"
        elif arguments.command == "plugin":
            params = _operation_params(arguments)
            action = arguments.action
        elif arguments.command == "migrate":
            params = _operation_params(arguments)
            action = f"migrate-{arguments.action}"
        else:
            raise ValueError(f"Unsupported command {arguments.command}")

        result = _response_json(
            client.post(
                "/_toolframe/v1/operations", json={"action": action, "params": params}
            )
        )
        no_wait = getattr(arguments, "no_wait", False)
        if not no_wait:
            result = _wait_for_operation(client, result)
        _emit_json(result)
        return _operation_exit_code(result, no_wait=no_wait)


def main(argv=None):
    try:
        arguments = parser().parse_args(argv)
        return run(arguments)
    except (PluginError, httpx.HTTPError, OSError, ValueError) as error:
        value = _failure_payload(error)
        print(json.dumps({"error": value}, ensure_ascii=False), file=sys.stderr)
        return _exit_code(_error_code(error))


if __name__ == "__main__":
    raise SystemExit(main())
