"""Versioned, project-local agent guidance with protection for customized copies."""

import fcntl
import hashlib
import json
import re
import shutil
import tempfile
import uuid
import zipfile
from importlib.resources import as_file, files
from pathlib import Path

import yaml
from toolframe_sdk import PluginError

SKILL_NAME = "toolframe-plugin-design"
RECEIPT = ".toolframe-install.json"


def bundled_skill():
    return files("toolframe_core").joinpath("bundled_skills", SKILL_NAME)


def _hashes(directory, *, include_receipt=False, exclude_evals=False):
    if not directory.is_dir() or directory.is_symlink():
        return None
    result = {}
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if exclude_evals and relative.parts[0] == "evals":
            continue
        if path.is_symlink():
            return None
        if path.is_file():
            relative = relative.as_posix()
            if relative != RECEIPT or include_receipt:
                result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif not path.is_dir():
            return None
    return result


def _record(destination):
    try:
        value = json.loads((destination / RECEIPT).read_text())
        if (
            value["format"] == 1
            and value["name"] == SKILL_NAME
            and re.fullmatch(r"\d+\.\d+\.\d+", value["version"])
            and isinstance(value["files"], dict)
            and value["files"]
            and all(
                isinstance(k, str) and isinstance(v, str)
                for k, v in value["files"].items()
            )
        ):
            return value
    except OSError, ValueError, TypeError, KeyError:
        pass
    return None


def _publish(source, destination, record, expected, *, backup):
    staging = Path(tempfile.mkdtemp(prefix=f".{SKILL_NAME}-", dir=destination.parent))
    previous = staging / "previous"
    published = False
    backup_path = None
    try:
        candidate = staging / "candidate"
        candidate.mkdir()
        # Publish exactly the files in the receipt, excluding development evals
        # even when this installer runs directly from a source checkout.
        for relative in record["files"]:
            target = candidate / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / relative, target)
        (candidate / RECEIPT).write_text(json.dumps(record, indent=2) + "\n")
        if destination.exists() or destination.is_symlink():
            if (
                expected is None
                or _hashes(destination, include_receipt=True) != expected
            ):
                raise PluginError(
                    "skill_conflict",
                    "Skill changed during installation; retry for a fresh comparison.",
                )
            destination.rename(previous)
            if _hashes(previous, include_receipt=True) != expected:
                raise PluginError(
                    "skill_conflict",
                    "Skill changed during installation; the original was retained.",
                )
            if backup:
                backup_path = (
                    destination.parent / f".{SKILL_NAME}-backup-{uuid.uuid4().hex}.zip"
                )
                with zipfile.ZipFile(backup_path, "x", zipfile.ZIP_DEFLATED) as archive:
                    for path in previous.rglob("*"):
                        if path.is_file():
                            archive.write(path, path.relative_to(previous))
        candidate.rename(destination)
        published = True
        return str(backup_path) if backup_path else None
    finally:
        if previous.exists() and not published:
            if destination.exists() or destination.is_symlink():
                # Keep both copies if an external writer claimed the destination.
                raise PluginError(
                    "skill_conflict", f"Original skill retained at {previous}"
                )
            previous.rename(destination)
        shutil.rmtree(staging)


def install_skill(
    home: Path, *, output=None, overwrite=False, confirm=None, upgrade_only=False
):
    destination = (output or home / ".agents" / "skills" / SKILL_NAME).absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Cooperating launches/installers must compare and publish under one lock.
    with (destination.parent / f".{SKILL_NAME}.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with as_file(bundled_skill()) as source:
            document = (source / "SKILL.md").read_text()
            match = re.match(
                r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", document, re.DOTALL
            )
            try:
                metadata = yaml.safe_load(match[1]) if match else None
            except yaml.YAMLError as error:
                raise ValueError(
                    "Bundled SKILL.md frontmatter is invalid YAML"
                ) from error
            version = metadata.get("version") if isinstance(metadata, dict) else None
            if not isinstance(version, str) or not re.fullmatch(
                r"\d+\.\d+\.\d+", version
            ):
                raise ValueError(
                    'Bundled SKILL.md frontmatter requires version: "x.x.x"'
                )
            record = {
                "format": 1,
                "name": SKILL_NAME,
                "version": version,
                "files": _hashes(source, exclude_evals=True),
            }
            result = {
                "name": SKILL_NAME,
                "path": str(destination),
                "bundled_version": version,
                "confirmation_required": False,
            }
            exists = destination.exists() or destination.is_symlink()
            current = _hashes(destination) if exists else None
            if exists and current is None:
                return {
                    **result,
                    "status": "preserved",
                    "reason": "non_regular_destination",
                }
            prior = _record(destination) if exists else None
            result["previous_version"] = prior["version"] if prior else None
            if exists and current == record["files"] and prior == record:
                return {**result, "status": "unchanged"}
            if upgrade_only and prior == record:
                return {
                    **result,
                    "status": "preserved",
                    "reason": "customized_current_version",
                }
            changed = exists and (prior is None or current != prior["files"])
            expected = _hashes(destination, include_receipt=True) if exists else None
            if changed and not overwrite:
                conflict = {
                    **result,
                    "status": "preserved",
                    "reason": "local_changes" if prior else "unmanaged_copy",
                    "confirmation_required": True,
                }
                if confirm is None:
                    return conflict
                if not confirm(conflict):
                    return {
                        **conflict,
                        "confirmation_required": False,
                        "reason": "user_declined",
                    }
            backup = _publish(source, destination, record, expected, backup=changed)
            return {
                **result,
                "status": "updated" if exists else "installed",
                "backup": backup,
            }


def installation_message(result):
    if result["status"] == "preserved":
        message = f"Kept existing skill at {result['path']} ({result['reason']})."
        if result["confirmation_required"]:
            message += " Run skill install interactively to review replacement, or use --overwrite after confirmation; --output PATH exports a separate copy."
        elif result["reason"] == "non_regular_destination":
            message += " Use --output PATH for a new regular directory."
        return message
    message = (
        f"Skill {result['status']} ({result['bundled_version']}): {result['path']}"
    )
    if result.get("backup"):
        message += f" Previous customized copy: {result['backup']}"
    return message
