"""Discover static authoring metadata and delegate explicit work to isolated wheels."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import shutil
import tempfile
import tomllib
import zipfile
from importlib.resources import files
from pathlib import Path

from jsonschema import Draft202012Validator
from packaging.utils import canonicalize_name
from referencing.exceptions import Unresolvable
from toolframe_sdk import PluginError, digest

from .authoring_metadata import from_wheel, installed_distributions
from .config import PackageSettings, atomic_write
from .packages import Packages, command, wheel_metadata


def package_settings(config, *, offline=False):
    settings = config.read()[0].packages if config.path.exists() else PackageSettings()
    extra = os.environ.get("TOOLFRAME_WHEELHOUSE")
    return settings.model_copy(
        update={
            "find_links": [*settings.find_links, *([extra] if extra else [])],
            "offline": offline
            or settings.offline
            or os.environ.get("TOOLFRAME_OFFLINE") == "1",
        }
    )


def available(config):
    """Read current Python metadata and cached application/authoring wheel receipts."""
    result = list(installed_distributions())
    root = config.home / ".toolframe"
    receipts = list((root / "authoring/available").glob("*.json"))
    if config.path.exists():
        # Source selection is configuration only. No state backend, host or token.
        project = tomllib.loads(config.path.read_text())
        receipts.extend(
            root / "source-resolutions" / f"{digest(plugin['source'])}.json"
            for plugin in project.get("plugins", {}).values()
            if isinstance(plugin, dict) and "source" in plugin
        )
    seen_paths = set()
    for receipt in receipts:
        if not receipt.is_file():
            continue
        artifact = json.loads(receipt.read_text())
        path = (config.home / artifact["artifact"]).resolve()
        if not path.is_relative_to(root.resolve()) or path in seen_paths:
            continue
        seen_paths.add(path)
        if (
            not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest()
            != artifact["artifact_hash"]
        ):
            raise PluginError(
                "artifact_hash_mismatch",
                f"Captured authoring wheel is missing or changed: {path}",
            )
        found = from_wheel(
            path,
            required=False,
            source={
                "kind": "cached",
                "request": artifact["source"]["request"],
                "sha256": artifact["artifact_hash"],
            },
        )
        if found:
            result.append(found)
    unique = {}
    for item in result:
        key = (item.distribution, item.version)
        if key in unique and (
            unique[key].manifest,
            unique[key].requires_python,
        ) != (item.manifest, item.requires_python):
            raise PluginError(
                "template_conflict",
                f"Conflicting authoring metadata for {item.distribution}=={item.version}; select an explicit --from wheel",
            )
        if key not in unique or item.wheel is not None:
            unique[key] = item
    return list(unique.values())


async def acquire(packages, source):
    artifact = await packages.capture(source, authoring=True)
    path = packages.home / artifact["artifact"]
    found = from_wheel(
        path,
        source={
            "kind": "wheel",
            "request": source,
            "sha256": artifact["artifact_hash"],
        },
    )
    atomic_write(
        packages.root / "authoring/available" / f"{found.distribution}.json",
        json.dumps(artifact),
    )
    return found


async def distributions(config, *, source=None):
    if source:
        return [await acquire(Packages(config.home, package_settings(config)), source)]
    return available(config)


async def listing(config, *, source=None, catalog=False):
    if catalog:
        if source:
            raise PluginError(
                "malformed_input", "--catalog and --from select different lists"
            )
        return {
            "templates": [],
            "catalog": json.loads(
                files("toolframe_core").joinpath("authoring_catalog.json").read_text()
            )["packages"],
        }
    return {
        "templates": sorted(
            [
                template
                for dist in await distributions(config, source=source)
                for template in dist.templates()
            ],
            key=lambda row: (row["id"], row["version"]),
        ),
        "catalog": [],
    }


async def select(config, identifier, *, source=None):
    if identifier.count("/") != 1:
        raise PluginError(
            "template_id_invalid", "Use a complete template ID: distribution/template"
        )
    name, template = identifier.split("/")
    identifier = f"{canonicalize_name(name)}/{template}"
    matches = [
        (dist, row)
        for dist in await distributions(config, source=source)
        for row in dist.templates()
        if row["id"] == identifier
    ]
    if len(matches) > 1:
        raise PluginError(
            "template_conflict",
            f"Multiple versions provide {identifier}; select one with --from",
        )
    if not matches:
        raise PluginError(
            "template_missing",
            f"Template {identifier} is not locally available. Use template list --catalog or specify --from wheel:PATH / pypi:PACKAGE==VERSION.",
        )
    return matches[0]


def snapshot_installed(dist, destination):
    """Repack the installed RECORD files, not a source tree or an index replacement.

    This lets Core's own template use the identical isolated wheel protocol even
    when the original installer did not retain its wheel archive.
    """
    installed = dist.installed
    direct = json.loads(installed.read_text("direct_url.json") or "{}")
    if direct.get("dir_info", {}).get("editable"):
        raise PluginError(
            "authoring_wheel_required",
            "Editable authoring packages require --from wheel:PATH",
        )
    wheel = installed.read_text("WHEEL") or ""
    tag = next(
        (
            line.removeprefix("Tag: ")
            for line in wheel.splitlines()
            if line.startswith("Tag: ")
        ),
        None,
    )
    if not tag or not installed.files:
        raise PluginError(
            "authoring_wheel_required",
            "Installed distribution lacks WHEEL/RECORD; use --from wheel:PATH",
        )
    output = (
        destination / f"{dist.distribution.replace('-', '_')}-{dist.version}-{tag}.whl"
    )
    records = []
    root = Path(installed.locate_file("")).resolve()
    record_name = None
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in installed.files:
            name = str(relative)
            path = Path(installed.locate_file(relative))
            if (
                ".." in relative.parts
                or relative.is_absolute()
                or name.endswith(".pyc")
            ):
                continue
            if name.endswith(".dist-info/RECORD"):
                record_name = name
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise PluginError(
                    "authoring_wheel_required",
                    "Installed authoring files include links; use an explicit wheel",
                )
            content = path.read_bytes()
            archive.writestr(name, content)
            checksum = (
                base64.urlsafe_b64encode(hashlib.sha256(content).digest())
                .rstrip(b"=")
                .decode()
            )
            records.append((name, "sha256=" + checksum, len(content)))
        if not record_name:
            raise PluginError(
                "authoring_wheel_required", "Installed distribution lacks RECORD"
            )
        output_record = io.StringIO()
        csv.writer(output_record).writerows([*records, (record_name, "", "")])
        archive.writestr(record_name, output_record.getvalue())
    return output


async def execute(packages, dist, request):
    dist.require_compatible()
    with tempfile.TemporaryDirectory(
        dir=packages.root / "tmp", prefix="authoring-"
    ) as directory:
        temporary = Path(directory)
        wheel = dist.wheel or snapshot_installed(dist, temporary)
        expected = dist.source.get("sha256")
        if expected and hashlib.sha256(wheel.read_bytes()).hexdigest() != expected:
            raise PluginError(
                "artifact_hash_mismatch", "Selected authoring wheel was modified"
            )
        captured = from_wheel(wheel)
        if (
            captured.distribution,
            captured.version,
            captured.manifest,
            captured.requires_python,
        ) != (
            dist.distribution,
            dist.version,
            dist.manifest,
            dist.requires_python,
        ):
            raise PluginError(
                "authoring_changed",
                "Authoring metadata changed after selection; retry with an explicit wheel",
            )
        stored, checksum = packages.artifact(wheel)
        artifact = {
            **wheel_metadata(stored, runtime=False),
            "artifact": str(stored.relative_to(packages.home)),
            "artifact_hash": checksum,
        }
        environment = await packages.environment(
            [artifact], extra="authoring", authoring=True
        )
        request_file = temporary / "request.json"
        request_file.write_text(json.dumps(request))
        # Copy the small stdlib-only protocol runner, never optional provider code.
        runner = temporary / "runner.py"
        runner.write_bytes(
            files("toolframe_core").joinpath("authoring_runner.py").read_bytes()
        )
        await command(
            packages.home / environment["python"],
            "-I",
            runner,
            request_file,
            cwd=temporary,
        )


def check_destination(destination):
    if destination.exists() or destination.is_symlink():
        raise PluginError(
            "scaffold_conflict",
            f"Refusing to overwrite existing target: {destination}",
            {"path": str(destination)},
        )


def publish(source, destination):
    check_destination(destination)
    rendered = {}
    for directory, names, filenames in os.walk(source, followlinks=False):
        for name in names + filenames:
            path = Path(directory) / name
            if path.is_symlink():
                raise PluginError(
                    "authoring_output_invalid",
                    "Generators must produce regular files and directories, not symlinks",
                )
            if path.is_file():
                rendered[path.relative_to(source)] = path.read_bytes()
    if not rendered:
        raise PluginError(
            "authoring_output_invalid", "Generator produced no project files"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.mkdir()
    except FileExistsError as error:
        raise PluginError(
            "scaffold_conflict", f"Refusing to overwrite existing target: {destination}"
        ) from error
    try:
        from toolframe_sdk.authoring import write_template

        write_template(destination, rendered)
    except BaseException:
        shutil.rmtree(destination)
        raise


async def new_plugin(
    config,
    name,
    *,
    template="toolframe-core/basic",
    source=None,
    output=None,
    parameters=None,
):
    from toolframe_sdk.authoring import plugin_names

    from .config import validate_plugin_id

    validate_plugin_id(name)
    destination = (
        output if output is not None else config.home / "plugins" / name
    ).absolute()
    check_destination(destination)
    dist, selected = await select(config, template, source=source)
    dist.require_compatible()
    parameters = {} if parameters is None else parameters
    if not isinstance(parameters, dict) or "name" in parameters:
        raise PluginError(
            "template_parameters_invalid",
            "--parameters must be a JSON object; supply name as the positional NAME",
        )
    parameters = {**parameters, "name": name}
    try:
        errors = list(
            Draft202012Validator(selected["parameters"]).iter_errors(parameters)
        )
    except (Unresolvable, RecursionError) as error:
        raise PluginError(
            "authoring_manifest_invalid",
            "Parameter schema has an unresolvable reference",
        ) from error
    if errors:
        raise PluginError("template_parameters_invalid", errors[0].message)
    packages = Packages(config.home, package_settings(config))
    with tempfile.TemporaryDirectory(
        dir=packages.root / "tmp", prefix="generate-"
    ) as directory:
        staged = Path(directory) / "project"
        staged.mkdir()
        await execute(
            packages,
            dist,
            {
                "operation": "generate",
                "entrypoint": selected["generator"],
                "destination": str(staged),
                "parameters": parameters,
            },
        )
        publish(staged, destination)
    package, module = plugin_names(name)
    return {
        "plugin_id": name,
        "package_name": package,
        "python_module": module,
        "path": str(destination),
        "status": "created",
        "template": selected,
    }


async def prepare_source(packages, source, destination, *, project):
    selection = (
        project.get("tool", {}).get("toolframe", {}).get("authoring", {}).get("source")
    )
    if not selection:
        from .web_build import legacy_source

        selection = legacy_source(project)
    dist = await acquire(packages, selection)
    entry = dist.manifest.get("prepare", {}).get("entrypoint")
    if not entry:
        raise PluginError(
            "authoring_prepare_missing",
            f"{dist.distribution} has no source preparation capability",
        )
    await execute(
        packages,
        dist,
        {
            "operation": "prepare",
            "entrypoint": entry,
            "source": str(source),
            "destination": str(destination),
            "cache": str(packages.root / "authoring/prepare-cache"),
            "offline": packages.settings.offline,
        },
    )


def generate(*, destination, parameters):
    """Core's basic template follows the same contract as every optional package."""
    from toolframe_sdk.authoring import render_plugin, write_template

    write_template(
        destination,
        render_plugin(
            files("toolframe_core").joinpath("basic_template"), parameters["name"]
        ),
    )
