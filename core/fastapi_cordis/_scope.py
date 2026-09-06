"""Resources and tasks owned by a single asyncio lifetime."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Coroutine
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from contextvars import ContextVar
from typing import Any, Self, TypeVar

from ._errors import PluginError

T = TypeVar("T")
_OWNER: ContextVar[tuple[Any, str] | None] = ContextVar(
    "cordis_work_owner", default=None
)


async def _complete(task: asyncio.Task[T]) -> T:
    """Finish owned cleanup before propagating cancellation of its waiter."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:  # noqa: BLE001 - task.result below propagates the original failure.
            break
    if cancelled:
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    return task.result()


class _Disposer:
    def __init__(self, scope: EffectScope, callback: Callable[[], Any]) -> None:
        self.scope = scope
        self.callback = callback
        self.task: asyncio.Task[None] | None = None

    async def __call__(self) -> None:
        self.scope._check_loop()
        if self.task is None:

            async def run() -> None:
                result = self.callback()
                if inspect.isawaitable(result):
                    await result

            self.task = self.scope._spawn(run())

            def finished(task: asyncio.Task[None]) -> None:
                if not task.cancelled() and task.exception() is None:
                    self.scope._cleanups.pop(self, None)

            self.task.add_done_callback(finished)
        if asyncio.current_task() is self.task:
            raise PluginError("A disposer cannot await itself")
        await _complete(self.task)


class EffectScope:
    """Own resources and tasks until a cancellation-safe, idempotent close.

    Mutations belong to one asyncio loop. Owned tasks are cancelled and joined
    before disposal callbacks run in reverse registration order. Every callback
    is attempted, and failures are reported together.
    """

    def __init__(
        self,
        *,
        _parent: EffectScope | None = None,
        _owner: tuple[Any, str] | None = None,
    ) -> None:
        self._parent = _parent
        self._owner = _parent._owner if _parent is not None else _owner
        self._loop: asyncio.AbstractEventLoop | None = None
        self._accepting = True
        self._cleanups: dict[_Disposer, None] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._task_errors: list[BaseException] = []
        self._acquisitions: set[asyncio.Task[Any]] = set()
        self._children: dict[EffectScope, None] = {}
        self._late_errors: list[BaseException] = []
        self._closing: asyncio.Task[None] | None = None

    def _check_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise PluginError("EffectScope belongs to another event loop")
        self._loop = loop

    def _is_open(self) -> bool:
        return self._accepting and (self._parent is None or self._parent._is_open())

    def _check_open(self) -> None:
        self._check_loop()
        if not self._is_open():
            raise PluginError("Cannot create resources in a closed effect scope")

    def _spawn(
        self, coroutine: Coroutine[Any, Any, T], *, name: str | None = None
    ) -> asyncio.Task[T]:
        token = _OWNER.set(self._owner)
        try:
            return asyncio.create_task(coroutine, name=name)
        finally:
            _OWNER.reset(token)

    @property
    def task_count(self) -> int:
        return len(self._tasks) + sum(
            child.task_count for child in tuple(self._children)
        )

    @property
    def task_error_count(self) -> int:
        return len(self._task_errors) + sum(
            child.task_error_count for child in tuple(self._children)
        )

    def on_dispose(
        self, callback: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Callable[[], Coroutine[Any, Any, None]]:
        """Register cleanup and return an idempotent async disposer."""
        self._check_open()
        if not callable(callback):
            raise TypeError("Cleanup must be callable")
        disposer = _Disposer(self, lambda: callback(*args, **kwargs))
        self._cleanups[disposer] = None
        return disposer

    def effect(
        self, register: Callable[[], Callable[[], Any]]
    ) -> Callable[[], Coroutine[Any, Any, None]]:
        """Run a synchronous registration returning sync or async cleanup.

        Use enter_async_context for asynchronous resource acquisition.
        """
        self._check_open()
        cleanup = register()
        if not callable(cleanup):
            if inspect.iscoroutine(cleanup):
                cleanup.close()
            raise TypeError("An effect must return a cleanup callable")
        return self.on_dispose(cleanup)

    def enter_context(self, resource: AbstractContextManager[T]) -> T:
        """Enter a context manager and own its eventual exit."""
        self._check_open()
        value = resource.__enter__()
        self.on_dispose(resource.__exit__, None, None, None)
        return value

    async def enter_async_context(self, resource: AbstractAsyncContextManager[T]) -> T:
        """Acquire an async resource, settling safely against close/cancellation."""
        self._check_open()

        async def acquire() -> T:
            value = await resource.__aenter__()
            if not self._is_open():
                try:
                    await resource.__aexit__(None, None, None)
                except BaseException as error:
                    self._late_errors.append(error)
                    raise
                raise PluginError("Effect scope closed during resource acquisition")
            self.on_dispose(resource.__aexit__, None, None, None)
            return value

        task = self._spawn(acquire())
        self._acquisitions.add(task)

        def finished(task: asyncio.Task[Any]) -> None:
            self._acquisitions.discard(task)
            if not task.cancelled():
                task.exception()

        task.add_done_callback(finished)
        return await _complete(task)

    def task(
        self, coroutine: Coroutine[Any, Any, T], *, name: str | None = None
    ) -> asyncio.Task[T]:
        """Start an owned task; close cancels and joins it before resources exit."""
        try:
            self._check_open()
            task = self._spawn(coroutine, name=name)
        except BaseException:
            if inspect.iscoroutine(coroutine):
                coroutine.close()
            raise
        self._tasks.add(task)

        def finished(task: asyncio.Task[Any]) -> None:
            self._tasks.discard(task)
            if not task.cancelled():
                error = task.exception()
                if error is not None:
                    self._task_errors.append(error)

        task.add_done_callback(finished)
        return task

    def scope(self) -> EffectScope:
        """Create a child resource scope owned by this scope."""
        self._check_open()
        child = EffectScope(_parent=self)
        self._children[child] = None
        return child

    async def _close(self) -> None:
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Acquisition continues through cancellation of its caller. Wait for its
        # late exit before reporting the scope closed.
        await asyncio.gather(*tuple(self._acquisitions), return_exceptions=True)
        errors = [*self._task_errors, *self._late_errors]
        for child in reversed(tuple(self._children)):
            try:
                await child.aclose()
            except BaseException as error:  # noqa: BLE001 - attempt all children before raising the group.
                errors.append(error)
        self._children.clear()
        for dispose in reversed(tuple(self._cleanups)):
            try:
                await dispose()
            except BaseException as error:  # noqa: BLE001 - attempt all cleanup before raising the group.
                errors.append(error)
        self._cleanups.clear()
        if errors:
            raise BaseExceptionGroup("Effect scope cleanup failed", errors)
        if self._parent is not None:
            self._parent._children.pop(self, None)

    def _owns_task(self, task: asyncio.Task[Any]) -> bool:
        return (
            task in self._tasks
            or task in self._acquisitions
            or task is self._closing
            or any(task is dispose.task for dispose in self._cleanups)
            or any(child._owns_task(task) for child in self._children)
        )

    async def aclose(self) -> None:
        self._check_loop()
        current = asyncio.current_task()
        if current is not None and self._owns_task(current):
            raise PluginError("An owned task or cleanup cannot close its own scope")
        if self._closing is None:
            self._accepting = False
            self._closing = asyncio.create_task(self._close())
        await _complete(self._closing)

    async def __aenter__(self) -> Self:
        self._check_open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
