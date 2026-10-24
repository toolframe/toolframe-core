"""Core authoring entry point delegates asset preparation to the Web package."""

import os
import shutil
from pathlib import Path

from .config import PackageSettings
from .packages import Packages


async def prepare(config, source, *, offline=False):
    source = Path(source).resolve()
    settings = config.read()[0].packages if config.path.exists() else PackageSettings()
    if os.environ.get("TOOLFRAME_WHEELHOUSE"):
        settings = settings.model_copy(
            update={
                "find_links": [*settings.find_links, os.environ["TOOLFRAME_WHEELHOUSE"]]
            }
        )
    settings = settings.model_copy(
        update={
            "offline": offline
            or settings.offline
            or os.environ.get("TOOLFRAME_OFFLINE") == "1"
        }
    )
    packages = Packages(config.home, settings)
    artifact = await packages.capture("path:" + str(source))
    wheel = config.home / artifact["artifact"]
    output = source / "build/web" / wheel.name
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(wheel, output)
    return {
        "wheel": str(output),
        "sha256": artifact["artifact_hash"],
        "source": str(source),
    }
