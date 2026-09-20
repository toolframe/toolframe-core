"""Explicit application recipes are data assets supplied by plugin distributions."""

import json
import os
import uuid
import zipfile
from importlib.resources import files
from pathlib import Path

import tomlkit
from toolframe_sdk import PluginError

from .config import Composition, PackageSettings, atomic_write
from .packages import Packages


async def initialize_template(config, template, *, name=None, host=None, port=9876):
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
    Composition.model_validate(document.unwrap())
    atomic_write(config.path, tomlkit.dumps(document))
