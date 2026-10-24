"""Core authoring entry point delegates asset preparation to the Web package."""

import shutil
from pathlib import Path

from .packages import Packages


async def prepare(config, source, *, offline=False):
    source = Path(source).resolve()
    from .authoring import package_settings

    settings = package_settings(config, offline=offline)
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


def legacy_source(project):
    """Compatibility for pre-manifest Web projects; preparation stays in the package."""
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    from toolframe_sdk import PluginError

    extras = project.get("project", {}).get("optional-dependencies", {})
    for requirement in extras.get("web", []):
        parsed = Requirement(requirement)
        if canonicalize_name(parsed.name) == "toolframe-web" and parsed.specifier:
            return "pypi:" + str(parsed)
    raise PluginError(
        "authoring_source_missing",
        "Declare [tool.toolframe.authoring] source = 'pypi:toolframe-web==VERSION' for Web preparation",
    )
