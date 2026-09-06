"""Typed references to services and their declared contracts."""

import re
from dataclasses import dataclass
from typing import Any, Generic, TypeVar, cast

T = TypeVar("T")
_SERVICE_NAME = re.compile(r"[a-zA-Z][a-zA-Z0-9_.-]*")


@dataclass(frozen=True)
class ServiceKey(Generic[T]):
    """Identify a service by name and the type it must implement."""

    name: str
    contract: type[T]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _SERVICE_NAME.fullmatch(self.name) is None:
            raise ValueError("service name must match [a-zA-Z][a-zA-Z0-9_.-]*")

        if not isinstance(self.contract, type):
            raise TypeError(
                "service contract must be a type supporting isinstance checks"
            )

        try:
            isinstance(None, self.contract)
        except TypeError as error:
            raise TypeError(
                "service contract must be a type supporting isinstance checks"
            ) from error

    def validate(self, value: Any) -> T:
        """Return a service when it implements this key's declared contract."""

        if not isinstance(value, self.contract):
            raise TypeError(
                f"service {self.name!r} does not implement its declared contract"
            )
        return cast(T, value)
