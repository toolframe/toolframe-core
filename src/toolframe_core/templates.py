"""Explicit application recipes are data assets supplied by plugin distributions."""

import hashlib
import json
import os
import uuid
import zipfile
from importlib.resources import files
from pathlib import Path

import tomlkit
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from toolframe_sdk import PluginError

from .config import Composition, HostLock, PackageSettings, atomic_write
from .lockfile import write_lock
from .packages import Packages, index_source
from .selection import prepare_selection
from .state import CoreState


async def initialize_template(config, template, *, name=None, host=None, port=9876):
    lock = HostLock(config.home)
    lock.acquire()
    try:
        await _initialize_template(config, template, name=name, host=host, port=port)
    finally:
        lock.close()


async def _initialize_template(config, template, *, name, host, port):
    if config.path.exists():
        raise PluginError(
            "configuration_exists", "Refusing to overwrite existing toolframe.toml"
        )
    local = Path(template)
    links = (
        [os.environ["TOOLFRAME_WHEELHOUSE"]]
        if os.environ.get("TOOLFRAME_WHEELHOUSE")
        else []
    )
    settings = PackageSettings(
        find_links=links, offline=os.environ.get("TOOLFRAME_OFFLINE") == "1"
    )
    artifact = None
    if local.is_file():
        document = tomlkit.parse(local.read_text())
    else:
        recipes = json.loads(
            files("toolframe_core").joinpath("templates.json").read_text()
        )
        if template not in recipes:
            raise PluginError(
                "template_unknown",
                "Choose a named template or a local composition TOML",
            )
        recipe = recipes[template]
        artifact = await Packages(config.home, settings).capture(recipe["source"])
        with zipfile.ZipFile(config.home / artifact["artifact"]) as wheel:
            document = tomlkit.parse(wheel.read(recipe["asset"]).decode())
    document["application"]["id"] = str(uuid.uuid4())
    document["application"]["port"] = port
    if host is not None:
        document["application"]["host"] = host
    if name:
        document["application"]["name"] = name
    if links:
        document.setdefault("packages", tomlkit.table())["find_links"] = links
    document.setdefault("access", tomlkit.table()).setdefault("failure_mode", "closed")
    composition = Composition.model_validate(document.unwrap())
    candidates = {}
    if artifact is not None:
        packages = Packages(config.home, composition.packages)
        packages.settings.offline |= settings.offline
        captured = {}
        owner = artifact["plugin_id"]
        if owner in composition.plugins:
            source = composition.plugins[owner].source
            if index_source(source):
                requirement = Requirement(source[5:])
                if (
                    canonicalize_name(requirement.name) == artifact["distribution"]
                    and artifact["version"] in requirement.specifier
                ):
                    captured[owner] = {
                        **artifact,
                        "source": {**artifact["source"], "request": source},
                    }
        candidates = await prepare_selection(
            packages,
            composition.plugins,
            composition.bindings,
            enabled={key for key, item in composition.plugins.items() if item.enabled},
            pin=True,
            artifacts=captured,
        )
        for plugin, candidate in candidates.items():
            document["plugins"][plugin]["source"] = candidate["source"]["request"]
        composition = Composition.model_validate(document.unwrap())
    text = tomlkit.dumps(document)
    if candidates:
        state = await CoreState.open(config.home)
        try:
            for candidate in candidates.values():
                await state.save_candidate(candidate)
            await write_lock(
                config.home,
                state,
                composition,
                hashlib.sha256(text.encode()).hexdigest(),
            )
        finally:
            await state.close()
    # Publish the composition only after all retained selections are durable.
    atomic_write(config.path, text)
