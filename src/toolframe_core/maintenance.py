"""Operator-only, read-only inventory of a stopped local application.

Keep :func:`stopped_application` open for the entire snapshot operation. The
serialized inventory describes files without configuration or credential values.
Protected environment locks can contain credentials and must remain private.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sqlite3
import stat
import subprocess
import tomllib
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from packaging.markers import Marker
from packaging.specifiers import SpecifierSet
from packaging.tags import sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version
from toolframe_sdk import PluginError

from .config import Composition, ConfigFile, HostLock, validate_plugin_id
from .state import StateSettings

__all__ = [
    "DependencyWheel",
    "EnvironmentLock",
    "InventoryFile",
    "RetainedKey",
    "SQLiteStore",
    "StoppedApplicationInventory",
    "application_lock",
    "stopped_application",
]

_HASH = re.compile(r"[a-f0-9]{64}")
_RUN = re.compile(r"[a-f0-9]{32}")
_DISTRIBUTION = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_LOCAL_SQL = "sql-sqlite"
_LOCAL_SQL_FACTORY = "toolframe_sql_sqlite.plugin:SqliteProvider"
_LOCAL_PROVIDERS = {
    "dashboard",
    "web",
    "mcp",
    "llms",
    "auth",
    "dashboard-auth",
    "mcp-auth",
    "oidc-auth",
}
_OMIT = {
    ".toolframe/host.lock",
    ".toolframe/endpoint.json",
    ".toolframe/environments",
    ".toolframe/tmp",
    ".toolframe/web-build-cache",
}


@dataclass(frozen=True)
class InventoryFile:
    path: str
    role: str
    size: int
    mode: int
    sha256: str | None = None


@dataclass(frozen=True)
class SQLiteStore:
    path: str
    owner: str | None
    provider: str


@dataclass(frozen=True)
class RetainedKey:
    path: str
    owner: str | None
    name: str


@dataclass(frozen=True)
class DependencyWheel:
    environment_id: str
    distribution: str
    version: str
    filename: str
    sha256: str
    path: str | None


@dataclass(frozen=True)
class EnvironmentLock:
    candidate_id: str
    purpose: str
    environment_id: str
    sha256: str
    python_version: str
    python_implementation: str
    platform_system: str
    platform_machine: str
    requires_python: str
    lock: str = field(repr=False)


@dataclass(frozen=True)
class StoppedApplicationInventory:
    home: Path
    application_id: str
    application_name: str
    composition_revision: str
    files: tuple[InventoryFile, ...]
    stores: tuple[SQLiteStore, ...]
    retained_keys: tuple[RetainedKey, ...]
    dependency_wheels: tuple[DependencyWheel, ...]
    environment_locks: tuple[EnvironmentLock, ...]
    format_version: int = 1
    status: str = "STOPPED"

    def as_dict(self) -> dict:
        """Return manifest metadata with home-relative paths and no secret values."""
        result = asdict(self)
        result.pop("home")
        # Arrays, rather than dataclass tuples, are part of the JSON contract.
        for name in (
            "files",
            "stores",
            "retained_keys",
            "dependency_wheels",
            "environment_locks",
        ):
            result[name] = list(result[name])
        for environment in result["environment_locks"]:
            environment.pop("lock")
        return result


def _fail(code="maintenance_layout_invalid", message="Application layout is invalid"):
    raise PluginError(code, message) from None


def _relative(value):
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or PurePosixPath(value).is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        _fail(message="Application paths must be safe home-relative paths")
    return value


def _path(home, value, *, required=True):
    relative = _relative(value)
    current = home
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            _fail(message="Symlinks are not supported in snapshot state")
    if required and not current.is_file():
        _fail(message="A required application file is missing")
    return current


def _json(text):
    return json.loads(text, parse_constant=lambda _: _fail())


@contextmanager
def _database(home, relative):
    path = _path(home, relative)
    for suffix in ("-wal", "-journal"):
        pending = _path(home, relative + suffix, required=False)
        if pending.exists() and (not pending.is_file() or pending.stat().st_size):
            _fail(
                "maintenance_recovery_required",
                "Cleanly stop SQLite and clear pending journals before maintenance",
            )
    # immutable avoids creating shared-memory/WAL files. Pending journals were
    # explicitly refused above, so no committed pages can be silently omitted.
    connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            _fail(message="SQLite integrity validation failed")
        yield connection
    finally:
        connection.close()


def _processes():
    try:
        result = subprocess.run(
            ["/bin/ps", "-axo", "pid=,pgid=,command="],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        rows = []
        seen = set()
        if result.returncode:
            _fail("maintenance_ownership_unproven", "Process inventory failed")
        for line in result.stdout.splitlines():
            parts = line.split(None, 2)
            if len(parts) != 3 or not all(part.isdigit() for part in parts[:2]):
                _fail(
                    "maintenance_ownership_unproven", "Process inventory is incomplete"
                )
            pid, pgid = map(int, parts[:2])
            # Linux kernel threads can have PGID 0; retain their commands too.
            if pid < 1 or pgid < 0 or pid in seen:
                _fail("maintenance_ownership_unproven", "Process inventory is invalid")
            seen.add(pid)
            rows.append(parts[2])
        if not rows:
            _fail("maintenance_ownership_unproven", "Process inventory is unavailable")
        return rows
    except OSError, subprocess.SubprocessError, UnicodeError:
        _fail(
            "maintenance_ownership_unproven", "Cannot verify worker process ownership"
        )


def _ownership(metadata, plugins, composition):
    run = metadata.get("host_run")
    owners = metadata.get("worker_owners", {})
    if (
        not isinstance(run, str)
        or not _RUN.fullmatch(run)
        or not isinstance(owners, dict)
    ):
        _fail(
            "maintenance_ownership_unproven",
            "Initialized worker ownership is unavailable",
        )
    # A stale entry is an unproven cleanup, even when its PID is absent. Recovery
    # belongs to Core's lifecycle, not a snapshot reader.
    if owners:
        _fail(
            "maintenance_ownership_unproven",
            "Worker cleanup must be reconciled before maintenance",
        )
    runs = {run}

    def observation(value, plugin):
        if not isinstance(value, dict):
            _fail("maintenance_ownership_unproven", "Worker observation is invalid")
        if value == {}:
            request = composition.plugins.get(plugin["id"])
            if (
                plugin["current_candidate"]
                or plugin["previous_candidate"]
                or (request is not None and request.enabled)
            ):
                _fail(
                    "maintenance_ownership_unproven",
                    "A selected worker has no durable stop observation",
                )
            return
        recorded = value.get("host_run")
        if not isinstance(recorded, str) or not _RUN.fullmatch(recorded):
            _fail("maintenance_ownership_unproven", "Worker observation is invalid")
        runs.add(recorded)
        if value.get("state") != "stopped":
            _fail(
                "maintenance_ownership_unproven",
                "Worker stop was not durably confirmed",
            )

    for plugin in plugins:
        observation(_json(plugin["runtime"]), plugin)
    for command in _processes():
        if "toolframe_sdk.worker" in command and any(
            re.search(r"(?:^|\s)--host-token\s+" + run + r"/", command) for run in runs
        ):
            _fail(
                "maintenance_ownership_unproven",
                "An application worker is still present",
            )


def _artifact(home, relative, expected, artifacts):
    if not isinstance(expected, str) or not _HASH.fullmatch(expected):
        _fail(message="Immutable artifact checksum is missing")
    relative = _relative(relative)
    if not relative.startswith(f".toolframe/artifacts/{expected}/"):
        _fail(message="Immutable artifacts must use the captured home layout")
    path = _path(home, relative, required=False)
    if not path.is_file():
        _fail(
            "maintenance_artifact_missing",
            "Capture every required dependency wheel before maintenance",
        )
    with path.open("rb") as source:
        checksum = hashlib.file_digest(source, "sha256").hexdigest()
    if checksum != expected:
        _fail(message="An immutable artifact checksum does not match")
    artifacts[relative] = checksum


def _environment(home, value, artifacts, wheels):
    if not isinstance(value, dict) or not isinstance(value.get("lock"), str):
        _fail(message="A retained environment lock is missing")
    identity = value.get("id")
    if not isinstance(identity, str) or not _HASH.fullmatch(identity):
        _fail(message="A retained environment identity is invalid")
    if value.get("python") != f".toolframe/environments/{identity}/bin/python":
        _fail(message="A retained environment path is invalid")
    lock = tomllib.loads(value["lock"])
    if lock.get("lock-version") != "1.0":
        _fail(message="A retained environment lock format is unsupported")
    compatibility = _compatibility(home, value, lock)
    supported_tags = set(sys_tags())
    packages = lock.get("packages")
    if not isinstance(packages, list) or not packages:
        _fail(message="A retained environment has no locked wheels")
    for package in packages:
        if not isinstance(package, dict) or any(
            field in package for field in ("directory", "vcs")
        ):
            _fail(
                "maintenance_artifact_missing",
                "Only fully captured wheel environments are supported",
            )
        direct = "archive" in package
        if direct:
            archive = package["archive"]
            if (
                not isinstance(archive, dict)
                or any(field in package for field in ("wheels", "sdist"))
                or "subdirectory" in archive
                or ("path" in archive) == ("url" in archive)
            ):
                _fail(message="A direct dependency wheel record is invalid")
            wheel = dict(archive)
            if "path" in archive:
                if not isinstance(archive["path"], str) or not archive["path"]:
                    _fail(message="A direct dependency wheel path is invalid")
                path = Path(archive["path"].replace("${TOOLFRAME_HOME}", str(home)))
                # Restrict direct paths to captured source-home artifacts. The
                # public lock stays exact, so restore consumers relocate paths.
                if not path.is_absolute():
                    _fail(message="A direct dependency wheel path must be absolute")
                wheel["url"] = path.as_uri()
            package_wheels = [wheel]
        else:
            package_wheels = package.get("wheels")
            if not isinstance(package_wheels, list) or not package_wheels:
                _fail(
                    "maintenance_artifact_missing",
                    "Only fully captured wheel environments are supported",
                )
        marker = package.get("marker")
        if marker is not None:
            if not isinstance(marker, str):
                _fail(message="A dependency marker is invalid")
            if not Marker(marker).evaluate():
                continue
        if not isinstance(package.get("name"), str) or not isinstance(
            package.get("version"), str
        ):
            _fail(message="A dependency identity is missing")
        if not _DISTRIBUTION.fullmatch(package["name"]):
            _fail(message="A dependency identity is invalid")
        version = Version(package["version"])
        compatible_wheel = False
        for wheel in package_wheels:
            if (
                not isinstance(wheel, dict)
                or not isinstance(wheel.get("url"), str)
                or not isinstance(wheel.get("hashes"), dict)
            ):
                _fail(message="A dependency wheel record is invalid")
            uri = urlsplit(wheel.get("url", "").replace("${TOOLFRAME_HOME}", str(home)))
            checksum = wheel.get("hashes", {}).get("sha256")
            filename = unquote(PurePosixPath(uri.path).name)
            _relative(filename)
            if "/" in filename:
                _fail(message="A dependency wheel filename must be a basename")
            if direct and not filename.endswith(".whl"):
                _fail(
                    "maintenance_artifact_missing",
                    "Direct dependencies must be captured wheel archives",
                )
            wheel_name, wheel_version, _, tags = parse_wheel_filename(filename)
            compatible_wheel |= bool(tags & supported_tags)
            if (
                wheel_name != canonicalize_name(package["name"])
                or wheel_version != version
            ):
                _fail(message="A dependency wheel differs from its locked package")
            if (
                not filename.endswith(".whl")
                or not isinstance(checksum, str)
                or not _HASH.fullmatch(checksum)
            ):
                _fail(message="A dependency wheel must have a locked SHA256")
            if uri.scheme == "file" and uri.netloc in ("", "localhost"):
                try:
                    relative = Path(unquote(uri.path)).relative_to(home).as_posix()
                except ValueError:
                    _fail(message="A dependency wheel escapes the application home")
            elif uri.scheme == "https":
                relative = f".toolframe/artifacts/{checksum}/{filename}"
                if not _path(home, relative, required=False).exists():
                    relative = None
            else:
                _fail(
                    "maintenance_artifact_missing",
                    "A dependency wheel is not captured locally",
                )
            if relative is not None:
                _artifact(home, relative, checksum, artifacts)
            elif direct:
                _fail(
                    "maintenance_artifact_missing",
                    "A direct dependency wheel is not captured locally",
                )
            item = DependencyWheel(
                identity,
                package["name"],
                package["version"],
                filename,
                checksum,
                relative,
            )
            wheels[(identity, filename, checksum)] = item
        if not compatible_wheel:
            _fail(
                message="Recorded dependency wheels do not support the source inspection platform"
            )
    return compatibility


def _compatibility(home, environment, lock):
    config = _path(home, f".toolframe/environments/{environment['id']}/pyvenv.cfg")
    settings = {}
    for line in config.read_text().splitlines():
        if not line.strip():
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or key in settings:
            _fail(message="Source interpreter metadata is invalid")
        settings[key] = value.strip()
    implementation = settings.get("implementation")
    inspector_version = Version(platform.python_version())
    versions = []
    for name in ("version_info", "version"):
        if name not in settings:
            continue
        if not re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", settings[name]):
            _fail(message="Source interpreter version metadata is invalid")
        recorded = Version(settings[name])
        precision = len(recorded.release)
        if (precision == 2 and recorded.release != inspector_version.release[:2]) or (
            precision == 3 and recorded != inspector_version
        ):
            _fail(message="Use a compatible source interpreter for offline maintenance")
        versions.append(recorded)
    if not versions or implementation != platform.python_implementation():
        _fail(message="Use a compatible source interpreter for offline maintenance")
    # uv can record only major/minor. Preserve that uncertainty in the public
    # metadata; use a full recorded version when either field supplies one.
    version = str(max(versions, key=lambda item: len(item.release)))
    requires_python = lock.get("requires-python", "")
    environments = lock.get("environments", [])
    if (
        not isinstance(requires_python, str)
        or not isinstance(environments, list)
        or any(not isinstance(marker, str) for marker in environments)
    ):
        _fail(message="Environment compatibility markers are invalid")
    if not SpecifierSet(requires_python).contains(inspector_version) or (
        environments and not any(Marker(marker).evaluate() for marker in environments)
    ):
        _fail(
            message="The recorded lock does not support the source inspection platform"
        )
    return (
        version,
        implementation,
        platform.system(),
        platform.machine(),
        requires_python,
    )


def _inventory(home):
    try:
        composition, revision = ConfigFile(home).read()
    except PluginError:
        _fail(message="Application composition is invalid")
    lock = _json(_path(home, "toolframe.lock").read_text())
    if (
        not isinstance(lock, dict)
        or lock.get("format") != 1
        or not isinstance(lock.get("plugins"), dict)
    ):
        _fail(message="Application lock format is unsupported")
    with _database(home, "data/core.sqlite") as database:
        database.row_factory = sqlite3.Row
        if database.execute("PRAGMA user_version").fetchone()[0] not in (1, 2):
            _fail(message="Initialized Core journal schema is unsupported")
        metadata = {
            row[0]: _json(row[1])
            for row in database.execute("SELECT key,value FROM metadata")
        }
        plugins = [
            dict(row) for row in database.execute("SELECT * FROM plugins ORDER BY id")
        ]
        _ownership(metadata, plugins, composition)
        if database.execute(
            "SELECT 1 FROM operations WHERE status IN ('queued','running','cancelling') LIMIT 1"
        ).fetchone() or metadata.get("forward_repair"):
            _fail(
                "maintenance_recovery_required",
                "Resolve interrupted management work before maintenance",
            )
        previous = metadata.get("last_valid_composition")
        if (
            not isinstance(previous, dict)
            or previous.get("application", {}).get("id") != composition.application.id
        ):
            _fail(message="Composition and initialized journal identities differ")
        desired_revision = lock.get("desired_revision")
        if (
            not isinstance(desired_revision, str)
            or not _HASH.fullmatch(desired_revision)
            or desired_revision != metadata.get("desired_revision")
        ):
            _fail(message="Composition lock and journal revisions differ")
        if revision != desired_revision:
            _fail(
                "maintenance_recovery_required",
                "Apply the current composition and cleanly stop Core before maintenance",
            )
        recorded = Composition.model_validate(previous)
        effective = composition.model_copy(deep=True)
        wheelhouse = os.environ.get("TOOLFRAME_WHEELHOUSE")
        if wheelhouse and wheelhouse not in effective.packages.find_links:
            effective.packages.find_links.append(wheelhouse)
        if os.environ.get("TOOLFRAME_OFFLINE") == "1":
            effective.packages.offline = True
        if recorded.model_dump() != effective.model_dump():
            _fail(
                message="Recorded composition differs from current runtime configuration"
            )
        if set(lock["plugins"]) != set(composition.plugins):
            _fail(message="Composition plugin coverage differs from its lock")
        candidates = {}
        for plugin in plugins:
            validate_plugin_id(plugin["id"])
            request = composition.plugins.get(plugin["id"])
            if request is not None and (
                not any(
                    plugin[selection]
                    for selection in (
                        "current_candidate",
                        "last_candidate",
                        "previous_candidate",
                    )
                )
                or (request.enabled and not plugin["current_candidate"])
            ):
                _fail(message="A configured plugin has no complete candidate selection")
            for selection in (
                "current_candidate",
                "previous_candidate",
                "last_candidate",
            ):
                identifier = plugin[selection]
                if identifier:
                    if not isinstance(identifier, str) or not _HASH.fullmatch(
                        identifier
                    ):
                        _fail(message="A retained candidate identity is invalid")
                    row = database.execute(
                        "SELECT data FROM candidates WHERE id=? AND plugin_id=?",
                        (identifier, plugin["id"]),
                    ).fetchone()
                    if row is None:
                        _fail(message="A retained candidate is missing")
                    candidate = _json(row[0])
                    if (
                        candidate.get("id") != identifier
                        or candidate.get("plugin_id") != plugin["id"]
                    ):
                        _fail(message="A retained candidate identity is invalid")
                    candidates[identifier] = candidate
                locked = lock["plugins"].get(plugin["id"], {}).get(selection)
                if plugin["id"] in lock["plugins"] and bool(locked) != bool(identifier):
                    _fail(message="A locked candidate differs from the journal")
                if locked and any(
                    locked.get(key) != candidate.get(key)
                    for key in (
                        "id",
                        "version",
                        "artifact",
                        "artifact_hash",
                        "source",
                        "environment",
                    )
                ):
                    _fail(message="A locked candidate differs from the journal")
                for field in ("ui_environment", "web_environment"):
                    if (
                        locked
                        and field in locked
                        and locked[field] != candidate.get(field)
                    ):
                        _fail(
                            message="A locked presentation environment differs from the journal"
                        )
        if set(composition.plugins) - {plugin["id"] for plugin in plugins}:
            _fail(message="A locked plugin is missing from the journal")

    selected = {plugin["current_candidate"] for plugin in plugins}
    artifacts, definitions, wheels, environments = {}, {}, {}, []
    for candidate in candidates.values():
        definition = candidate["definition"]
        if (
            not isinstance(definition, dict)
            or definition.get("id") != candidate["plugin_id"]
        ):
            _fail(message="A retained plugin definition is invalid")
        kind = definition.get("provider_kind")
        if (
            kind == "web"
            and candidate["id"] in selected
            and not candidate.get("web_environment")
        ):
            _fail(
                message="A selected Web provider has no recorded presentation environment"
            )
        if (
            definition.get("runtime_validation") == "separate_application"
            or (
                kind == "sql"
                and (
                    definition["id"] != _LOCAL_SQL
                    or definition.get("factory") != _LOCAL_SQL_FACTORY
                )
            )
            or (kind is not None and kind not in _LOCAL_PROVIDERS | {"sql"})
        ):
            _fail(
                "maintenance_external_state",
                "External or unknown provider-managed state cannot be captured by local maintenance",
            )
        definitions.setdefault(definition["id"], []).append(definition)
        _artifact(home, candidate["artifact"], candidate["artifact_hash"], artifacts)
        for purpose, environment_field in (
            ("backend", "environment"),
            ("ui", "ui_environment"),
            ("web", "web_environment"),
        ):
            if environment_field == "environment" or candidate.get(environment_field):
                environment = candidate[environment_field]
                compatibility = _environment(home, environment, artifacts, wheels)
                environments.append(
                    EnvironmentLock(
                        candidate["id"],
                        purpose,
                        environment["id"],
                        hashlib.sha256(environment["lock"].encode()).hexdigest(),
                        *compatibility,
                        environment["lock"],
                    )
                )

    bindings = metadata.get("store_bindings", {})
    if not isinstance(bindings, dict):
        _fail(message="Retained store bindings are invalid")
    stores = [SQLiteStore("data/core.sqlite", None, "core")]
    keys = []
    for owner, provider in bindings.items():
        validate_plugin_id(owner)
        if (
            owner not in definitions
            or provider != _LOCAL_SQL
            or provider not in definitions
        ):
            _fail(
                "maintenance_external_state",
                "A retained store has external or uncertain ownership",
            )
        relative = f"data/plugins/{owner}/data.sqlite"
        with _database(home, relative) as database:
            identity = database.execute(
                "SELECT owner,application_id FROM _toolframe_identity"
            ).fetchall()
            if identity != [(owner, composition.application.id)]:
                _fail(message="A business store identity differs from the application")
        stores.append(SQLiteStore(relative, owner, provider))
    for owner, values in definitions.items():
        for name in {
            name
            for definition in values
            for name in definition.get("private_secrets", [])
        }:
            validate_plugin_id(name)
            relative = f".toolframe/secrets/{owner}/{name}.key"
            path = _path(home, relative, required=False)
            if path.exists():
                if not path.is_file() or path.stat().st_mode & 0o077:
                    _fail(
                        message="Retained private keys require owner-only regular files"
                    )
                keys.append(RetainedKey(relative, owner, name))
            elif owner in bindings:
                _fail(message="A retained business store private key is missing")

    roles = {"toolframe.toml": "composition", "toolframe.lock": "lock"}
    roles.update(
        {
            item.path: "core_store" if item.owner is None else "business_store"
            for item in stores
        }
    )
    roles.update({path: "artifact" for path in artifacts})
    roles.update({item.path: "retained_key" for item in keys})
    files = []
    omitted = _OMIT | {
        store.path + suffix
        for store in stores
        for suffix in ("-shm", "-wal", "-journal")
    }

    def walk(directory):
        for path in sorted(directory.iterdir()):
            relative = path.relative_to(home).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                _fail(message="Symlinks are not supported in snapshot state")
            parts = PurePosixPath(relative).parts
            if (
                len(parts) == 4
                and parts[:2] == ("data", "plugins")
                and parts[3]
                in {
                    "data.sqlite",
                    "data.sqlite-wal",
                    "data.sqlite-journal",
                    "data.sqlite-shm",
                }
                and "/".join((*parts[:3], "data.sqlite")) not in roles
            ):
                _fail(
                    "maintenance_external_state",
                    "A business store has no retained provider binding",
                )
            if relative in omitted or stat.S_ISSOCK(mode):
                continue
            if stat.S_ISDIR(mode):
                walk(path)
            elif stat.S_ISREG(mode):
                role = roles.get(relative, "home_file")
                if relative == ".toolframe/management.token" or relative.startswith(
                    ".toolframe/secrets/"
                ):
                    role = "retained_key"
                    if mode & 0o077:
                        _fail(
                            message="Retained credentials require owner-only regular files"
                        )
                    if relative not in {key.path for key in keys}:
                        keys.append(
                            RetainedKey(
                                relative,
                                None,
                                "management"
                                if relative.endswith("management.token")
                                else path.stem,
                            )
                        )
                files.append(
                    InventoryFile(
                        relative,
                        role,
                        path.stat().st_size,
                        stat.S_IMODE(mode),
                        artifacts.get(relative),
                    )
                )
            else:
                _fail(message="Only regular in-home snapshot files are supported")

    walk(home)
    return StoppedApplicationInventory(
        home,
        composition.application.id,
        composition.application.name,
        revision,
        tuple(files),
        tuple(stores),
        tuple(sorted(keys, key=lambda item: item.path)),
        tuple(wheels[key] for key in sorted(wheels)),
        tuple(environments),
    )


def _home(home):
    home = Path(os.path.abspath(Path(home).expanduser()))
    if home.is_symlink() or not home.is_dir():
        _fail(message="An existing regular application home is required")
    return home.resolve()


@contextmanager
def application_lock(home: str | Path):
    """Exclusively lock an existing home, without asserting worker/state safety.

    Creates only HostLock's directory/PID file. A restore consumer may keep this
    context open while moving its own new staging home and rebuilding runtimes.
    Use ``stopped_application`` to validate and snapshot an initialized source.
    """
    home = _home(home)
    directory = _path(home, ".toolframe", required=False)
    if directory.exists() and not directory.is_dir():
        _fail(message="The application lock directory must be a regular directory")
    lock_path = _path(home, ".toolframe/host.lock", required=False)
    if lock_path.exists() and not lock_path.is_file():
        _fail(message="The application lock must be a regular file")
    lock = HostLock(home)
    try:
        try:
            lock.acquire()
        except OSError:
            _fail(message="The application lock cannot be acquired")
        yield None
    finally:
        lock.close()


@contextmanager
def stopped_application(home: str | Path):
    """Hold the real HostLock while inventorying and snapshotting a stopped home.

    Raises ``PluginError`` for running, uncertain, external, incomplete or unsafe
    layouts. This never starts workers, repairs state, or migrates databases.
    ``home`` must have completed a Core startup and clean shutdown.
    """
    home = _home(home)
    if StateSettings.from_env().backend != "sqlite":
        _fail(
            "maintenance_external_state",
            "External Core state is unsupported by local maintenance",
        )
    for relative in (
        "toolframe.toml",
        "toolframe.lock",
        "data/core.sqlite",
        ".toolframe/management.token",
    ):
        _path(home, relative)
    with application_lock(home):
        try:
            inventory = _inventory(home)
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            sqlite3.Error,
            RecursionError,
        ):
            # Parser/driver errors can contain stored credentials; expose only
            # the stable operator error, never persisted values or SQL rows.
            _fail()
        yield inventory
