"""Exercise owned resources and local events without adding a product worker."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core"))

from fastapi_cordis import EffectScope, EventKey, Plugin, PluginManager

NOTE = EventKey("example.note", str)


async def exercise():
    received, disposed = [], []
    ready = asyncio.Event()

    async def setup(ctx):
        child: EffectScope = ctx.scope()
        child.on_dispose(disposed.append, "child resource")
        ctx.on(NOTE, received.append)

        async def wait_until_stopped():
            ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                disposed.append("task stopped")

        ctx.task(wait_until_stopped(), name="example-owned-wait")
        return "ready"

    manager = PluginManager([Plugin("example.observer", setup)])
    manager.validate()
    async with manager.lifespan():
        await ready.wait()
        assert manager.snapshot()[0]["ownedTasks"] == 1
        await manager.emit(NOTE, "first")
        assert received == ["first"]
        await manager.disable("example.observer")
        assert disposed == ["task stopped", "child resource"]
        await manager.emit(NOTE, "while disabled")
        assert received == ["first"]
        ready.clear()
        await manager.enable("example.observer")
        await ready.wait()
        await manager.emit(NOTE, "after restart")
        assert received == ["first", "after restart"]
    assert disposed == ["task stopped", "child resource"] * 2

    async with EffectScope() as scope:
        dispose = scope.effect(lambda: lambda: disposed.append("standalone resource"))
        await dispose()
        await dispose()
    assert disposed.count("standalone resource") == 1


if __name__ == "__main__":
    asyncio.run(exercise())
    print("Owned tasks, child cleanup, idempotent disposal and local events passed.")
