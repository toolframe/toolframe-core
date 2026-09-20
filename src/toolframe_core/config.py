"""TOML is desired state. Every managed edit is revision checked and atomically replaced."""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import secrets
import tempfile
import uuid
from pathlib import Path
from typing import Literal

import tomlkit
from pydantic import BaseModel, ConfigDict, Field, field_validator
from toolframe_sdk import PluginError


def validate_plugin_id(identifier: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", identifier):
        raise ValueError(
            "Plugin IDs must be normalized lowercase slugs (1–64 characters)"
        )
    return identifier


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Application(Model):
    id: str
    name: str = "Toolframe"
    host: str = "127.0.0.1"
    port: int = Field(default=9876, ge=0, le=65535)

    @field_validator("host")
    @classmethod
    def local_host(cls, value):
        if value != "localhost" and not ipaddress.ip_address(value).is_loopback:
            raise ValueError(
                "v1 management/gateway listeners are local-operator loopback only"
            )
        return value


class PluginRequest(Model):
    source: str
    enabled: bool = False
    watch: bool = False
    config: dict = Field(default_factory=dict)


class RuntimeSettings(Model):
    drain_timeout: float = Field(default=30, gt=0)
    activation_timeout: float = Field(default=30, gt=0)
    disposal_timeout: float = Field(default=10, gt=0)
    health_interval: float = Field(default=10, gt=0)
    health_ttl: float = Field(default=30, gt=0)
    watch_debounce_ms: int = Field(default=500, ge=50)


class PackageSettings(Model):
    find_links: list[str] = Field(default_factory=list)
    index_url: str = "https://pypi.org/simple"
    offline: bool = False


class Composition(Model):
    schema_version: Literal[1] = 1
    application: Application
    plugins: dict[str, PluginRequest] = Field(default_factory=dict)
    bindings: dict[str, str] = Field(default_factory=dict)
    required_capabilities: list[str] = Field(default_factory=list)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    packages: PackageSettings = Field(default_factory=PackageSettings)

    @field_validator("plugins")
    @classmethod
    def plugin_ids(cls, value):
        for identifier in value:
            validate_plugin_id(identifier)
        return value


def atomic_write(path: Path, content: str, *, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ConfigFile:
    def __init__(self, home: Path):
        self.home = home.resolve()
        self.path = self.home / "toolframe.toml"

    @property
    def revision(self):
        return (
            hashlib.sha256(self.path.read_bytes()).hexdigest()
            if self.path.exists()
            else None
        )

    def read(self):
        try:
            text = self.path.read_text()
            document = tomlkit.parse(text)
            return Composition.model_validate(document.unwrap()), hashlib.sha256(
                text.encode()
            ).hexdigest()
        except (OSError, ValueError) as error:
            raise PluginError("configuration_invalid", str(error)) from error

    def initialize(self, *, name="Toolframe", port=9876):
        if self.path.exists():
            raise PluginError(
                "configuration_exists", "Refusing to overwrite existing toolframe.toml"
            )
        self.home.mkdir(parents=True, exist_ok=True)
        value = Composition(
            application=Application(id=str(uuid.uuid4()), name=name, port=port)
        )
        atomic_write(self.path, tomlkit.dumps(value.model_dump()))
        return value

    def edit(self, expected_revision, change):
        if self.revision != expected_revision:
            raise PluginError("stale_plan", "toolframe.toml changed after planning")
        document = tomlkit.parse(self.path.read_text())
        change(document)
        value = Composition.model_validate(document.unwrap())
        # Recheck immediately before rename; host lock serializes managed writers.
        if self.revision != expected_revision:
            raise PluginError(
                "stale_plan", "toolframe.toml changed while applying the edit"
            )
        atomic_write(self.path, tomlkit.dumps(document))
        return value, self.revision

    def put_plugin(self, identifier, request, expected_revision):
        def change(document):
            if "plugins" not in document:
                document["plugins"] = tomlkit.table()
            document["plugins"][identifier] = request.model_dump()

        return self.edit(expected_revision, change)


class HostLock:
    def __init__(self, home):
        self.path = Path(home) / ".toolframe" / "host.lock"
        self.file = None

    def acquire(self):
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self.file.close()
            self.file = None
            raise PluginError(
                "host_already_running", "Another core owns this application home"
            ) from error
        self.file.seek(0)
        self.file.truncate()
        self.file.write(str(os.getpid()))
        self.file.flush()

    def close(self):
        if self.file:
            self.file.close()
            self.file = None


def management_token(home):
    path = Path(home) / ".toolframe" / "management.token"
    if not path.exists():
        atomic_write(path, secrets.token_urlsafe(32))
    return path.read_text().strip()
