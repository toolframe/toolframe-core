"""Typed in-process event dispatch."""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar, cast

from ._scope import _complete

T = TypeVar("T")
_EVENT_NAME = re.compile(r"[a-zA-Z][a-zA-Z0-9_.-]*")


def _validate_payload_type(payload_type: object) -> None:
    if payload_type is Any or not isinstance(payload_type, type):
        raise TypeError("payload_type must be a runtime type")
    if getattr(payload_type, "_is_protocol", False) and not getattr(
        payload_type, "_is_runtime_protocol", False
    ):
        raise TypeError("payload_type must be a runtime-checkable type")
    try:
        isinstance(None, payload_type)
    except TypeError as error:
        raise TypeError("payload_type must support isinstance") from error


@dataclass(frozen=True)
class EventKey(Generic[T]):
    """A name and runtime payload type identifying one event contract."""

    name: str
    payload_type: type[T]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str):
            raise TypeError("Event name must be a string")
        if _EVENT_NAME.fullmatch(self.name) is None:
            raise ValueError(f"Invalid event name: {self.name!r}")
        _validate_payload_type(self.payload_type)

    def validate(self, payload: object) -> T:
        """Validate and return a payload without exposing its representation."""
        try:
            valid = isinstance(payload, self.payload_type)
        except TypeError:
            raise TypeError(f"Invalid payload for event {self.name!r}") from None
        if not valid:
            raise TypeError(f"Invalid payload for event {self.name!r}")
        return cast(T, payload)


@dataclass(eq=False)
class _Registration:
    callback: Callable[[Any], Any]


async def _invoke(callback: Callable[[Any], Any], payload: Any) -> BaseException | None:
    try:
        result = callback(payload)
        if inspect.isawaitable(result):
            await result
    except BaseException as error:  # noqa: BLE001 - callback failures are aggregated.
        return error
    return None


class _EventBus:
    """An asyncio-owned bus with stable, name-scoped payload contracts."""

    def __init__(self) -> None:
        self._contracts: dict[str, type[Any]] = {}
        self._handlers: dict[str, list[_Registration]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def _check_key(self, key: EventKey[Any]) -> None:
        if not isinstance(key, EventKey):
            raise TypeError("key must be an EventKey")

    def _check_contract(self, key: EventKey[Any]) -> None:
        contract = self._contracts.get(key.name)
        if contract is not None and contract is not key.payload_type:
            raise TypeError(
                f"Event {key.name!r} already has a different payload contract"
            )

    def _establish_contract(self, key: EventKey[Any]) -> None:
        self._contracts.setdefault(key.name, key.payload_type)

    def on(self, key: EventKey[T], callback: Callable[[T], Any]) -> Callable[[], None]:
        self._owner_loop()
        self._check_key(key)
        if not callable(callback):
            raise TypeError("callback must be callable")
        self._check_contract(key)
        self._establish_contract(key)
        registration = _Registration(cast(Callable[[Any], Any], callback))
        handlers = self._handlers.setdefault(key.name, [])
        handlers.append(registration)

        def unsubscribe() -> None:
            self._owner_loop()
            if registration not in handlers:
                return
            registration_index = next(
                index for index, item in enumerate(handlers) if item is registration
            )
            handlers.pop(registration_index)

        return unsubscribe

    def _owner_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("Event bus belongs to a different asyncio loop")
        return loop

    async def emit(self, key: EventKey[T], payload: T) -> None:
        loop = self._owner_loop()
        self._check_key(key)
        self._check_contract(key)
        validated_payload = key.validate(payload)
        self._establish_contract(key)
        callbacks = tuple(
            registration.callback for registration in self._handlers.get(key.name, ())
        )
        if not callbacks:
            return

        tasks = [
            loop.create_task(_invoke(callback, validated_payload))
            for callback in callbacks
        ]

        async def join():
            return await asyncio.gather(*tasks, return_exceptions=True)

        joined = loop.create_task(join())
        try:
            results = await asyncio.shield(joined)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await _complete(joined)
            raise

        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise BaseExceptionGroup(f"Event {key.name!r} callback failures", failures)
