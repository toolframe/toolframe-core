"""Capture sources and resolve content-addressed, non-editable worker environments."""

from __future__ import annotations

import asyncio
import configparser
import email
import fnmatch
import hashlib
import json
import os
import platform
import shutil
import signal
import sys
import tempfile
import tomllib
import urllib.parse
import zipfile
from pathlib import Path

import httpx
import tomlkit
from packaging.requirements import Requirement
from packaging.tags import sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename
from toolframe_sdk import (
    PROTOCOL_VERSION,
    PluginDefinition,
    PluginError,
    digest,
    validate,
)

from .config import atomic_write
from .process import WorkerProcess

IGNORED = {
    ".git",
    ".venv",
    ".toolframe",
    "data",
    "dist",
    "build",
    "target",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".env",
}


async def command(*arguments, cwd=None, timeout=300):
    """No shell, bounded diagnostics, and owned child process group on cancellation."""
    process = await asyncio.create_subprocess_exec(
        *map(str, arguments),
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    tail = bytearray()

    async def consume():
        while chunk := await process.stdout.read(8192):
            tail.extend(chunk)
            del tail[:-32768]
        return await process.wait()

    try:
        async with asyncio.timeout(timeout):
            result = await consume()
    except BaseException:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
        raise
    output = tail.decode(errors="replace")
    if result:
        raise PluginError(
            "package_command_failed",
            output,
            {"executable": str(arguments[0]), "exit_code": result},
        )
    return output


def source_files(root):
    patterns = []
    ignore = root / ".toolframeignore"
    if ignore.is_file():
        patterns = [
            line.strip()
            for line in ignore.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
    files = {}
    for directory, names, filenames in os.walk(root, followlinks=False):
        names[:] = sorted(name for name in names if name not in IGNORED)
        for name in names + sorted(filenames):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if (
                name in IGNORED
                or name.endswith((".pyc", ".pyo"))
                or any(fnmatch.fnmatch(relative, p) for p in patterns)
            ):
                if name in names:
                    names.remove(name)
                continue
            if path.is_symlink():
                raise PluginError(
                    "source_symlink",
                    f"Source symlinks must be materialized: {relative}",
                )
            if path.is_file():
                data = path.read_bytes()
                files[relative] = (
                    hashlib.sha256(data).hexdigest(),
                    bool(path.stat().st_mode & 0o111),
                )
    return files


def snapshot(root, destination):
    root = root.resolve()
    if not (root / "pyproject.toml").is_file():
        raise PluginError(
            "source_invalid", "Local/Git plugin must contain pyproject.toml"
        )
    before = source_files(root)
    identifier = digest(before)
    target = destination / identifier
    with tempfile.TemporaryDirectory(dir=destination, prefix="capture-") as temporary:
        copied = Path(temporary)
        for name, (_, executable) in before.items():
            path = copied / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((root / name).read_bytes())
            path.chmod(0o755 if executable else 0o644)
        if before != source_files(copied) or before != source_files(root):
            raise PluginError(
                "source_changed", "Source changed during capture; validate again"
            )
        if not target.exists():
            os.rename(copied, target)
    return target, identifier


def wheel_metadata(path):
    with zipfile.ZipFile(path) as wheel:
        metadata = [
            name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata) != 1:
            raise PluginError("wheel_invalid", "Wheel must contain one distribution")
        message = email.message_from_bytes(wheel.read(metadata[0]))
        entryfile = metadata[0].removesuffix("METADATA") + "entry_points.txt"
        entries = configparser.ConfigParser(interpolation=None)
        entries.optionxform = str
        if entryfile not in wheel.namelist():
            raise PluginError(
                "entrypoint_missing", "Wheel has no toolframe.plugins entrypoint"
            )
        entries.read_string(wheel.read(entryfile).decode())
        group = (
            dict(entries["toolframe.plugins"])
            if entries.has_section("toolframe.plugins")
            else {}
        )
        if len(group) != 1:
            raise PluginError(
                "entrypoint_invalid",
                "One distribution must expose exactly one toolframe.plugins entrypoint",
            )
        identifier, entrypoint = next(iter(group.items()))
        return {
            "distribution": canonicalize_name(message["Name"]),
            "version": message["Version"],
            "plugin_id": identifier,
            "entrypoint": entrypoint,
            "extras": message.get_all("Provides-Extra", []),
        }


class Packages:
    def __init__(self, home, settings):
        self.home, self.settings = Path(home).resolve(), settings
        self.root = self.home / ".toolframe"
        for name in ("sources", "artifacts", "environments", "tmp"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.uv = shutil.which("uv")
        if not self.uv:
            raise PluginError("uv_missing", "The launcher or PATH must provide uv")

    def flags(self):
        result = ["--index-url", self.settings.index_url]
        for link in self.settings.find_links:
            result += [
                "--find-links",
                str((self.home / link).resolve()) if "://" not in link else link,
            ]
        if self.settings.offline:
            result += ["--offline"]
        return result

    def artifact(self, path):
        data = path.read_bytes()
        identifier = hashlib.sha256(data).hexdigest()
        target = self.root / "artifacts" / identifier / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(data)
        return target, identifier

    async def download(self, url, target, expected=None):
        parts = urllib.parse.urlsplit(url)
        if parts.username or parts.password or parts.scheme != "https":
            raise PluginError(
                "source_url_invalid",
                "Remote artifacts require HTTPS without embedded credentials",
            )
        if self.settings.offline:
            raise PluginError(
                "offline_artifact_missing",
                "Artifact is not captured locally; offline mode cannot download it",
            )
        sha = hashlib.sha256()
        size = 0
        async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:  # noqa: SIM117
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                with target.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 512 * 1024 * 1024:
                            raise PluginError(
                                "artifact_too_large", "Artifact exceeds 512 MiB"
                            )
                        sha.update(chunk)
                        handle.write(chunk)
        if expected and sha.hexdigest() != expected:
            raise PluginError(
                "artifact_hash_mismatch",
                "Downloaded artifact differs from its pinned SHA256",
            )

    async def fetch_locked(self, item, target):
        expected = item.get("hashes", {}).get("sha256")
        url = item["url"]
        if url.startswith("file://"):
            shutil.copyfile(
                Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)), target
            )
        else:
            await self.download(url, target, expected)
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if expected and actual != expected:
            raise PluginError(
                "artifact_hash_mismatch", "Resolved artifact changed before capture"
            )
        return actual

    async def _build(self, source, destination):
        await command(
            self.uv,
            "--no-config",
            "build",
            "--no-sources",
            "--wheel",
            "--python",
            sys.executable,
            "--out-dir",
            destination,
            *self.flags(),
            source,
        )
        wheels = list(destination.glob("*.whl"))
        if len(wheels) != 1:
            raise PluginError("build_invalid", "Build must produce exactly one wheel")
        return wheels[0]

    async def capture(self, source):
        cached = self.root / "source-resolutions" / f"{digest(source)}.json"
        if (
            self.settings.offline
            and cached.exists()
            and source.startswith(("pypi:", "git+", "https://"))
        ):
            value = json.loads(cached.read_text())
            if (
                hashlib.sha256((self.home / value["artifact"]).read_bytes()).hexdigest()
                != value["artifact_hash"]
            ):
                raise PluginError(
                    "artifact_hash_mismatch", "Cached source artifact was modified"
                )
            return value
        with tempfile.TemporaryDirectory(dir=self.root / "tmp") as temporary:
            temporary = Path(temporary)
            provenance = {"request": source}
            if source.startswith("git+"):
                if self.settings.offline:
                    raise PluginError(
                        "offline_source_missing",
                        "Use the existing locked candidate offline; Git capture needs a fetch",
                    )
                url, _, fragment = source[4:].partition("#")
                # A ref follows the last @ after the URL authority (SSH URLs are also supported).
                authority_end = (
                    url.find("/", url.find("://") + 3)
                    if "://" in url
                    else url.find(":")
                )
                ref = "HEAD"
                if url.rfind("@") > authority_end:
                    url, ref = url.rsplit("@", 1)
                checkout = temporary / "checkout"
                await command("git", "clone", "--no-checkout", "--", url, checkout)
                try:
                    commit = (
                        await command(
                            "git",
                            "rev-parse",
                            "--verify",
                            f"{ref}^{{commit}}",
                            cwd=checkout,
                        )
                    ).strip()
                except PluginError:
                    commit = (
                        await command(
                            "git",
                            "rev-parse",
                            "--verify",
                            f"origin/{ref}^{{commit}}",
                            cwd=checkout,
                        )
                    ).strip()
                await command("git", "checkout", "--detach", commit, cwd=checkout)
                subdirectory = urllib.parse.parse_qs(fragment).get(
                    "subdirectory", [""]
                )[0]
                root = (checkout / subdirectory).resolve()
                if not root.is_relative_to(checkout):
                    raise PluginError(
                        "source_invalid", "Git subdirectory escapes checkout"
                    )
                captured, source_hash = await asyncio.to_thread(
                    snapshot, root, self.root / "sources"
                )
                provenance.update(kind="git", commit=commit, source_hash=source_hash)
                wheel = await self._build(captured, temporary / "wheel")
            elif source.startswith("https://"):
                url, _, fragment = source.partition("#")
                name = Path(urllib.parse.urlsplit(url).path).name
                if not name.endswith(".whl"):
                    raise PluginError(
                        "source_invalid", "Direct HTTPS sources must be wheels"
                    )
                wheel = temporary / name
                expected = urllib.parse.parse_qs(fragment).get("sha256", [None])[0]
                await self.download(url, wheel, expected)
                provenance.update(kind="wheel")
            elif source.startswith("pypi:"):
                requirement = str(Requirement(source.removeprefix("pypi:")))
                requirements = temporary / "requirements.in"
                requirements.write_text(requirement + "\n")
                lockfile = temporary / "pylock.toml"
                await command(
                    self.uv,
                    "--no-config",
                    "pip",
                    "compile",
                    "--no-deps",
                    "--format",
                    "pylock.toml",
                    "--python",
                    sys.executable,
                    "--output-file",
                    lockfile,
                    *self.flags(),
                    requirements,
                )
                packages = tomllib.loads(lockfile.read_text())["packages"]
                if len(packages) != 1:
                    raise PluginError(
                        "source_invalid", "PyPI source must resolve one distribution"
                    )
                package = packages[0]
                compatible = set(sys_tags())
                chosen = None
                for item in package.get("wheels", []):
                    name = Path(urllib.parse.urlsplit(item["url"]).path).name
                    if compatible & parse_wheel_filename(name)[3]:
                        chosen = item
                        break
                if chosen is None:
                    chosen = package.get("sdist")
                    if not chosen:
                        raise PluginError(
                            "artifact_unavailable",
                            "Index package has no compatible wheel or source archive",
                        )
                    archive = (
                        temporary / Path(urllib.parse.urlsplit(chosen["url"]).path).name
                    )
                    provenance["sdist_hash"] = await self.fetch_locked(chosen, archive)
                    self.artifact(archive)
                    wheel = await self._build(archive, temporary / "wheel")
                else:
                    wheel = (
                        temporary / Path(urllib.parse.urlsplit(chosen["url"]).path).name
                    )
                    await self.fetch_locked(chosen, wheel)
                provenance.update(
                    kind="pypi",
                    distribution=package["name"],
                    version=package["version"],
                )
            else:
                raw = source.removeprefix("path:").removeprefix("wheel:")
                path = (self.home / raw).resolve()
                if path.is_dir():
                    captured, source_hash = await asyncio.to_thread(
                        snapshot, path, self.root / "sources"
                    )
                    provenance.update(
                        kind="local", path=str(path), source_hash=source_hash
                    )
                    wheel = await self._build(captured, temporary / "wheel")
                elif path.is_file() and path.suffix == ".whl":
                    wheel = temporary / path.name
                    shutil.copyfile(path, wheel)
                    provenance.update(kind="wheel", path=str(path))
                else:
                    raise PluginError("source_missing", f"Unknown local source: {path}")
            metadata = wheel_metadata(wheel)
            stored, wheel_hash = self.artifact(wheel)
            result = {
                **metadata,
                "artifact": str(stored.relative_to(self.home)),
                "artifact_hash": wheel_hash,
                "source": provenance,
            }
            atomic_write(cached, json.dumps(result))
            return result

    async def environment(self, artifacts, *, ui=False):
        requirements = []
        names = set()
        for artifact in artifacts:
            name = artifact["distribution"]
            if name in names:
                raise PluginError(
                    "duplicate_distribution",
                    f"Conflicting distributions in environment: {name}",
                )
            names.add(name)
            extra = "ui" if ui else "backend"
            suffix = f"[{extra}]" if extra in artifact.get("extras", []) else ""
            path = self.home / artifact["artifact"]
            if (
                hashlib.sha256(path.read_bytes()).hexdigest()
                != artifact["artifact_hash"]
            ):
                raise PluginError(
                    "artifact_hash_mismatch", "Captured wheel was modified"
                )
            requirements.append(f"{name}{suffix} @ {path.as_uri()}")
        # The unpublished development SDK is supplied explicitly through the configured wheelhouse.
        if "toolframe-sdk" not in names:
            for link in self.settings.find_links:
                if "://" in link:
                    continue
                matches = sorted((self.home / link).glob("toolframe_sdk-0.1.0-*.whl"))
                if matches:
                    sdk, _ = self.artifact(matches[-1])
                    requirements.append(f"toolframe-sdk @ {sdk.as_uri()}")
                    break
        with tempfile.TemporaryDirectory(dir=self.root / "tmp") as temporary:
            temporary = Path(temporary)
            input_file = temporary / "requirements.in"
            input_file.write_text("\n".join(requirements) + "\n")
            lockfile = temporary / "pylock.toml"
            await command(
                self.uv,
                "--no-config",
                "pip",
                "compile",
                "--no-header",
                "--format",
                "pylock.toml",
                "--python",
                sys.executable,
                "--output-file",
                lockfile,
                *self.flags(),
                input_file,
            )
            document = tomlkit.parse(lockfile.read_text())
            for package in document.get("packages", []):
                for wheel in package.get("wheels", []):
                    if wheel.get("url", "").startswith("file://"):
                        path = Path(
                            urllib.parse.unquote(
                                urllib.parse.urlsplit(wheel["url"]).path
                            )
                        )
                        stored, checksum = self.artifact(path)
                        wheel["url"] = stored.as_uri()
                        wheel["hashes"] = {"sha256": checksum}
            lock = tomlkit.dumps(document).replace(str(self.home), "${TOOLFRAME_HOME}")
            key = digest(
                {
                    "lock": lock,
                    "python": sys.version,
                    "platform": platform.platform(),
                    "ui": ui,
                }
            )
            target = self.root / "environments" / key
            if not (target / ".ready").exists():
                if target.exists():
                    shutil.rmtree(target)
                await command(
                    self.uv, "--no-config", "venv", "--python", sys.executable, target
                )
                expanded = target / "pylock.toml"
                expanded.write_text(lock.replace("${TOOLFRAME_HOME}", str(self.home)))
                await command(
                    self.uv,
                    "--no-config",
                    "pip",
                    "sync",
                    "--python",
                    target / "bin/python",
                    *self.flags(),
                    expanded,
                )
                atomic_write(target / ".ready", key)
            return {
                "id": key,
                "python": str((target / "bin/python").relative_to(self.home)),
                "lock": lock,
            }

    async def prepare(self, identifier, source, config):
        artifact = await self.capture(source)
        if artifact["plugin_id"] != identifier:
            raise PluginError(
                "identity_mismatch",
                "Entry point name differs from configured plugin ID",
            )
        environment = await self.environment([artifact])

        async def unavailable(method, params):
            raise PluginError(
                "preflight_only", "Preflight cannot access application services"
            )

        worker = await WorkerProcess.start(
            self.home / environment["python"],
            artifact["entrypoint"],
            unavailable,
            cwd=self.home,
        )
        try:
            report = await worker.request("runtime.describe")
            definition = PluginDefinition.model_validate(report["definition"])
            if report["protocol"] != PROTOCOL_VERSION or definition.id != identifier:
                raise PluginError(
                    "identity_mismatch",
                    "Worker definition/protocol does not match the installed entrypoint",
                )
            validate(definition.config_schema, config)
        finally:
            try:
                await worker.close(activated=False)
            except BaseException:
                await (
                    worker.force_stop()
                )  # isolated preflight has no application state or live work
                raise
        candidate = {
            **artifact,
            "environment": environment,
            "definition": definition.model_dump(mode="json"),
            "documents": report["documents"],
            "config": config,
            "validation": {"preflight": "passed", "runtime": "not_run"},
        }
        candidate["id"] = digest(
            {key: value for key, value in candidate.items() if key != "validation"}
        )
        return candidate
