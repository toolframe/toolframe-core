"""Static distribution authoring metadata; this module never loads entry points."""

from __future__ import annotations

import email
import json
import re
import sys
import zipfile
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from jsonschema import Draft202012Validator, SchemaError
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import (
    InvalidWheelFilename,
    canonicalize_name,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version
from toolframe_sdk import PluginError

from . import __version__

MANIFEST = "extra_metadata/toolframe-authoring.json"
MAX_MANIFEST = 1024 * 1024
ENTRY = r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*"


def invalid(message):
    return PluginError("authoring_manifest_invalid", message)


def _object(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys():
        raise invalid(f"Expected authoring object with {', '.join(required)}")
    if value.keys() - set(required) - set(optional):
        raise invalid("Unknown authoring metadata fields")


def _local_schema(value):
    # Discovery and validation must never retrieve a remote JSON Schema.
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"$ref", "$dynamicRef"} and (
                not isinstance(child, str) or not child.startswith("#")
            ):
                raise invalid("Parameter schemas may only use local references")
            _local_schema(child)
    elif isinstance(value, list):
        for child in value:
            _local_schema(child)


def parse_manifest(raw, *, distribution, version, requires_python=None):
    try:
        Version(version)
    except (InvalidVersion, TypeError) as error:
        raise invalid("Distribution metadata must contain a valid version") from error
    if len(raw.encode()) > MAX_MANIFEST:
        raise invalid("Authoring manifest exceeds 1 MiB")
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise invalid("Authoring manifest must be UTF-8 JSON") from error
    _object(document, ("schema_version", "requires_core", "templates"), ("prepare",))
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise PluginError(
            "authoring_version_incompatible",
            "Unsupported authoring schema_version; expected 1",
        )
    try:
        core_range = SpecifierSet(document["requires_core"])
        python_range = SpecifierSet(requires_python or "")
    except (InvalidSpecifier, TypeError) as error:
        raise invalid("Invalid Core or Python version requirement") from error
    if (
        __version__ not in core_range
        or ".".join(map(str, sys.version_info[:3])) not in python_range
    ):
        raise PluginError(
            "authoring_version_incompatible",
            f"{distribution}=={version} requires Core {core_range} and Python {python_range}",
        )
    if not isinstance(document["templates"], list):
        raise invalid("templates must be a list")
    seen = set()
    for template in document["templates"]:
        _object(template, ("id", "title", "description", "parameters", "generator"))
        if not isinstance(template["id"], str) or not re.fullmatch(
            r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", template["id"]
        ):
            raise invalid("Template id must use lowercase letters, numbers and hyphens")
        if template["id"] in seen:
            raise PluginError(
                "template_conflict",
                f"Duplicate template ID: {distribution}/{template['id']}",
            )
        seen.add(template["id"])
        if any(
            not isinstance(template[key], str) or not template[key].strip()
            for key in ("title", "description")
        ):
            raise invalid("Template title and description must be nonempty strings")
        if not isinstance(template["generator"], str) or not re.fullmatch(
            ENTRY, template["generator"]
        ):
            raise invalid("Generator must be a module:function reference")
        schema = template["parameters"]
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise invalid("Parameter schema must describe an object")
        _local_schema(schema)
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as error:
            raise invalid(f"Invalid parameter schema: {error.message}") from error
    if "prepare" in document:
        _object(document["prepare"], ("entrypoint",))
        if not isinstance(document["prepare"]["entrypoint"], str) or not re.fullmatch(
            ENTRY, document["prepare"]["entrypoint"]
        ):
            raise invalid("Preparation entry must be a module:function reference")
    return document


@dataclass
class AuthoringDistribution:
    distribution: str
    version: str
    manifest: dict
    source: dict
    wheel: Path | None = None
    installed: object | None = None

    def templates(self):
        return [
            {
                **template,
                "id": f"{self.distribution}/{template['id']}",
                "distribution": self.distribution,
                "version": self.version,
                "source": self.source,
                "schema_version": self.manifest["schema_version"],
                "requires_core": self.manifest["requires_core"],
            }
            for template in self.manifest["templates"]
        ]


def from_wheel(path, *, source=None, required=True):
    path = Path(path)
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise invalid("Wheel contains duplicate members")
            metas = [name for name in names if name.endswith(".dist-info/METADATA")]
            if len(metas) != 1:
                raise invalid("Wheel must contain exactly one distribution")
            meta = email.message_from_bytes(archive.read(metas[0]))
            name = metas[0].removesuffix("METADATA") + MANIFEST
            if name not in names:
                if not required:
                    return None
                raise PluginError(
                    "authoring_missing",
                    f"{meta['Name']}=={meta['Version']} has no authoring manifest",
                )
            if archive.getinfo(name).file_size > MAX_MANIFEST:
                raise invalid("Authoring manifest exceeds 1 MiB")
            distribution = canonicalize_name(meta["Name"])
            version = meta["Version"]
            wheel_name, wheel_version, _, _ = parse_wheel_filename(path.name)
            if wheel_name != distribution or Version(version) != wheel_version:
                raise invalid("Wheel filename and distribution metadata disagree")
            manifest = parse_manifest(
                archive.read(name).decode("utf-8"),
                distribution=distribution,
                version=version,
                requires_python=meta["Requires-Python"],
            )
    except (
        zipfile.BadZipFile,
        KeyError,
        UnicodeError,
        TypeError,
        InvalidVersion,
        InvalidWheelFilename,
    ) as error:
        raise invalid(f"Invalid authoring wheel: {path.name}") from error
    return AuthoringDistribution(
        distribution,
        version,
        manifest,
        source or {"kind": "wheel", "path": str(path)},
        wheel=path,
    )


def installed_distributions():
    for dist in metadata.distributions():
        raw = dist.read_text(MANIFEST)
        if raw is None:
            continue
        name = canonicalize_name(dist.metadata["Name"])
        manifest = parse_manifest(
            raw,
            distribution=name,
            version=dist.version,
            requires_python=dist.metadata.get("Requires-Python"),
        )
        yield AuthoringDistribution(
            name,
            dist.version,
            manifest,
            {"kind": "installed", "location": str(dist.locate_file(""))},
            installed=dist,
        )
