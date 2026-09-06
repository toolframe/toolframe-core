"""Typed, startup-only database settings for Toolframe."""

from __future__ import annotations

import hashlib
import keyword
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

__all__ = ["DatabaseConfigurationError", "DatabaseSettings"]


class DatabaseConfigurationError(ValueError):
    """Database settings could not be loaded from the requested sources."""


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class _PostgresTarget(tuple[str, int | None, str]):
    __slots__ = ()

    @property
    def hostname(self) -> str:
        return self[0]

    @property
    def port(self) -> int | None:
        return self[1]

    @property
    def database(self) -> str:
        return self[2]


def _invalid_path() -> ValueError:
    return ValueError("invalid sqlite path")


def _normalize_path(value: Any, *, base: Path | None = None) -> Path:
    if isinstance(value, str) and not value.strip():
        raise _invalid_path()

    try:
        path = Path(value).expanduser()
        if base is not None and not path.is_absolute():
            path = base / path
        return path.resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError):
        raise _invalid_path() from None


def _valid_identifier(component: str) -> bool:
    return bool(
        _IDENTIFIER_RE.fullmatch(component)
        and component.isascii()
        and not keyword.iskeyword(component)
    )


def _validate_provider_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid database provider")  # noqa: TRY004
    if value in {"sqlite", "postgresql"}:
        return value

    provider_parts = value.split(":")
    if len(provider_parts) != 2:
        raise ValueError("invalid database provider")
    module_name, factory_name = provider_parts
    module_parts = module_name.split(".")
    if not all(
        _valid_identifier(part) for part in module_parts
    ) or not _valid_identifier(factory_name):
        raise ValueError("invalid database provider")
    return value


def _decode_database_path(path: str) -> str:
    if not path.startswith("/"):
        raise ValueError("invalid postgres dsn")
    encoded_database = path.lstrip("/")
    if not encoded_database:
        raise ValueError("invalid postgres dsn")
    for index, character in enumerate(encoded_database):
        if character == "%" and (
            index + 2 >= len(encoded_database)
            or not all(
                digit in "0123456789abcdefABCDEF"
                for digit in encoded_database[index + 1 : index + 3]
            )
        ):
            raise ValueError("invalid postgres dsn")
    try:
        database = unquote(encoded_database, encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError):
        raise ValueError("invalid postgres dsn") from None
    if not database:
        raise ValueError("invalid postgres dsn")
    return database


def _validate_url_text(value: str) -> None:
    for index, character in enumerate(value):
        if ord(character) < 32 or ord(character) == 127 or character.isspace():
            raise ValueError("invalid postgres dsn")
        if character == "%" and (
            index + 2 >= len(value)
            or not all(
                digit in "0123456789abcdefABCDEF"
                for digit in value[index + 1 : index + 3]
            )
        ):
            raise ValueError("invalid postgres dsn")


def _parse_postgres_dsn(value: str) -> _PostgresTarget:
    try:
        _validate_url_text(value)
        parsed = urlsplit(value)
        if parsed.scheme not in {"postgresql", "postgresql+psycopg"} or parsed.fragment:
            raise ValueError("invalid postgres dsn")
        hostname = parsed.hostname
        if not hostname or any(character.isspace() for character in hostname):
            raise ValueError("invalid postgres dsn")

        host_port = parsed.netloc.rsplit("@", 1)[-1]
        has_port = False
        if host_port.startswith("["):
            closing_bracket = host_port.find("]")
            if closing_bracket < 0:
                raise ValueError("invalid postgres dsn")
            remainder = host_port[closing_bracket + 1 :]
            if remainder:
                if not remainder.startswith(":") or not remainder[1:]:
                    raise ValueError("invalid postgres dsn")
                has_port = True
        elif ":" in host_port:
            port_text = host_port.rsplit(":", 1)[1]
            if not port_text:
                raise ValueError("invalid postgres dsn")
            has_port = True

        port = parsed.port
        if has_port and (port is None or not 1 <= port <= 65535):
            raise ValueError("invalid postgres dsn")
        database = _decode_database_path(parsed.path)
    except (AttributeError, IndexError, TypeError, ValueError):
        raise ValueError("invalid postgres dsn") from None
    return _PostgresTarget((hostname, port, database))


def _normalize_dsn(value: Any) -> SecretStr:
    if isinstance(value, SecretStr):
        dsn = value.get_secret_value()
    elif isinstance(value, str):
        dsn = value
    else:
        raise ValueError("invalid postgres dsn")  # noqa: TRY004
    if not dsn:
        raise ValueError("invalid postgres dsn")
    _parse_postgres_dsn(dsn)
    return SecretStr(dsn)


def _invalid_configuration() -> DatabaseConfigurationError:
    return DatabaseConfigurationError("invalid database configuration")


def _redact_validation_error(error: ValidationError, title: str) -> ValidationError:
    redacted_errors = []
    for details in error.errors(include_context=True, include_url=True):
        redacted = dict(details)
        redacted["input"] = None
        redacted_errors.append(redacted)
    return ValidationError.from_exception_data(title, redacted_errors)


class DatabaseSettings(BaseModel):
    """Immutable database settings resolved before Toolframe startup."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        allow_inf_nan=False,
    )

    provider: str = "sqlite"
    sqlite_path: Path
    postgres_dsn: SecretStr | None = Field(default=None, repr=False)
    timeout_seconds: float = Field(default=5.0, gt=0, le=300)
    pool_size: int = Field(default=5, ge=1, le=128)
    pool_timeout_seconds: float = Field(default=5.0, gt=0, le=300)
    max_rows: int = Field(default=10000, gt=0)
    max_bytes: int = Field(default=10485760, gt=0)

    def __init__(self, **data: Any) -> None:
        validation_error: ValidationError | None = None
        try:
            super().__init__(**data)
        except ValidationError as error:
            validation_error = _redact_validation_error(error, type(self).__name__)
        if validation_error is not None:
            raise validation_error

    @field_validator("provider", mode="before")
    @classmethod
    def _provider_is_supported(cls, value: Any) -> str:
        return _validate_provider_name(value)

    @field_validator("sqlite_path", mode="before")
    @classmethod
    def _path_is_normalized(cls, value: Any) -> Path:
        return _normalize_path(value)

    @field_validator("postgres_dsn", mode="before")
    @classmethod
    def _dsn_is_valid(cls, value: Any) -> SecretStr | None:
        if value is None:
            return None
        return _normalize_dsn(value)

    @field_validator(
        "timeout_seconds",
        "pool_size",
        "pool_timeout_seconds",
        "max_rows",
        "max_bytes",
        mode="before",
    )
    @classmethod
    def _booleans_are_not_numbers(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError(  # noqa: TRY004
                "boolean values are not valid numeric settings"
            )
        return value

    @model_validator(mode="after")
    def _postgres_requires_dsn(self) -> DatabaseSettings:
        if self.provider == "postgresql" and self.postgres_dsn is None:
            raise ValueError("postgresql provider requires postgres_dsn")
        return self

    @classmethod
    def from_env(
        cls,
        *,
        root: Path,
        cwd: Path | None = None,
        cli_database: Path | str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> DatabaseSettings:
        """Resolve settings from CLI, environment, and the repository root."""

        environment = os.environ if environ is None else environ
        pending_error: DatabaseConfigurationError | None = None
        try:
            root_path = _normalize_path(root)
            cwd_path = _normalize_path(Path.cwd() if cwd is None else cwd)

            if cli_database is not None:
                sqlite_value: Any = cli_database
                sqlite_base = cwd_path
            elif "TOOLFRAME_SQLITE_PATH" in environment:
                sqlite_value = environment["TOOLFRAME_SQLITE_PATH"]
                sqlite_base = cwd_path
            elif "TOOLFRAME_DB" in environment:
                sqlite_value = environment["TOOLFRAME_DB"]
                sqlite_base = cwd_path
            else:
                sqlite_value = root_path / "data.db"
                sqlite_base = None

            sqlite_path = _normalize_path(sqlite_value, base=sqlite_base)
            values: dict[str, Any] = {"sqlite_path": sqlite_path}

            if "TOOLFRAME_DB_PROVIDER" in environment:
                values["provider"] = environment["TOOLFRAME_DB_PROVIDER"]
            if "TOOLFRAME_POSTGRES_DSN" in environment:
                values["postgres_dsn"] = environment["TOOLFRAME_POSTGRES_DSN"]

            env_numeric_fields: tuple[tuple[str, str, type[float | int]], ...] = (
                ("TOOLFRAME_DB_TIMEOUT", "timeout_seconds", float),
                ("TOOLFRAME_DB_POOL_SIZE", "pool_size", int),
                ("TOOLFRAME_DB_POOL_TIMEOUT", "pool_timeout_seconds", float),
                ("TOOLFRAME_DB_MAX_ROWS", "max_rows", int),
                ("TOOLFRAME_DB_MAX_BYTES", "max_bytes", int),
            )
            for environment_name, field_name, parser in env_numeric_fields:
                if environment_name not in environment:
                    continue
                raw_value = environment[environment_name]
                if not isinstance(raw_value, str) or not raw_value.strip():
                    raise ValueError("invalid numeric setting")
                try:
                    values[field_name] = parser(raw_value.strip())
                except (TypeError, ValueError, OverflowError):
                    raise ValueError("invalid numeric setting") from None

            return cls(**values)
        except DatabaseConfigurationError as error:
            pending_error = error
        except (OSError, RuntimeError, TypeError, ValueError, ValidationError):
            pending_error = _invalid_configuration()

        if pending_error is not None:
            raise pending_error
        raise _invalid_configuration()

    def public_info(self) -> dict[str, Any]:
        """Return non-secret settings suitable for diagnostics."""

        if self.provider == "sqlite":
            target = str(self.sqlite_path)
        elif self.provider == "postgresql":
            postgres_target = _parse_postgres_dsn(self.postgres_dsn.get_secret_value())
            host = postgres_target.hostname
            if ":" in host:
                host = f"[{host}]"
            target = f"{host}:{postgres_target.port or 5432}/{postgres_target.database}"
        else:
            target = "configured by custom provider"
        return {
            "provider": self.provider,
            "target": target,
            "timeoutSeconds": self.timeout_seconds,
            "poolSize": self.pool_size,
            "poolTimeoutSeconds": self.pool_timeout_seconds,
            "maxRows": self.max_rows,
            "maxBytes": self.max_bytes,
        }

    @property
    def identity(self) -> str:
        """Return a stable, non-secret identity for the selected target."""

        components = [self.provider]
        if self.provider == "sqlite":
            components.extend(("sqlite_path", str(self.sqlite_path)))
        elif self.provider == "postgresql":
            components.extend(
                (
                    "postgres_dsn",
                    self.postgres_dsn.get_secret_value(),
                )
            )
        else:
            components.extend(("sqlite_path", str(self.sqlite_path)))
            components.extend(
                (
                    "postgres_dsn",
                    self.postgres_dsn.get_secret_value()
                    if self.postgres_dsn is not None
                    else "<none>",
                )
            )
        payload = "\0".join(f"{len(component)}:{component}" for component in components)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
