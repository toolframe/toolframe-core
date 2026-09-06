"""FastAPI adapter. Management routes are opt-in; host apps own authorization."""

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request

from ._runtime import (
    ConfigurationError,
    PluginError,
    PluginManager,
    PluginUnavailable,
    UnknownPlugin,
)
from ._services import ServiceKey


def service(name: str | ServiceKey[Any], *, scope: str = ""):
    """Use with Depends(service('name')); holds the lease through response work."""

    async def dependency(request: Request):
        manager: PluginManager = request.app.state.plugin_manager
        try:
            with manager.lease(name, scope=scope) as value:
                yield value
        except PluginUnavailable as error:
            raise HTTPException(503, str(error)) from error

    return dependency


def plugin_router(
    manager: PluginManager, *, prefix: str = "/plugins", management: bool = False
) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["plugins"])

    @router.get("")
    async def list_plugins() -> dict[str, Any]:
        return {"plugins": manager.snapshot()}

    async def apply(operation, *, snapshot: bool = True) -> dict[str, Any]:
        try:
            result = await operation
        except UnknownPlugin as error:
            raise HTTPException(404, "Unknown plugin") from error
        except ConfigurationError as error:
            # Validation errors may include secrets from submitted configuration.
            raise HTTPException(422, "Invalid plugin configuration") from error
        except PluginError as error:
            raise HTTPException(409, str(error)) from error
        except Exception as error:
            logging.getLogger("fastapi_cordis").exception("Plugin operation failed")
            raise HTTPException(
                500, "Plugin operation failed; inspect plugin states and server logs"
            ) from error
        return {"plugins": manager.snapshot()} if snapshot else result

    if management:

        @router.post("/{name:path}/enable")
        async def enable(name: str) -> dict[str, Any]:
            return await apply(manager.enable(name))

        @router.post("/{name:path}/disable")
        async def disable(name: str, cascade: bool = False) -> dict[str, Any]:
            return await apply(manager.disable(name, cascade=cascade))

        @router.post("/{name:path}/retry")
        async def retry(name: str) -> dict[str, Any]:
            return await apply(manager.retry(name))

        @router.put("/{name:path}/config")
        async def configure(name: str, config: dict[str, Any]) -> dict[str, Any]:
            return await apply(manager.configure(name, config))

        @router.post("/{name:path}/config/plan")
        async def plan(name: str, config: dict[str, Any]) -> dict[str, Any]:
            async def preview():
                return manager.plan_configure(name, config)

            return await apply(preview(), snapshot=False)

    return router


def install(
    app: FastAPI,
    manager: PluginManager,
    *,
    prefix: str = "/plugins",
    management: bool = False,
) -> None:
    """Compose with the existing lifespan; start plugins first and stop them last."""
    if hasattr(app.state, "plugin_manager"):
        raise PluginError("A plugin manager is already installed")
    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        async with manager.lifespan(), previous(application) as state:
            yield state

    app.state.plugin_manager = manager
    app.router.lifespan_context = lifespan
    app.include_router(plugin_router(manager, prefix=prefix, management=management))
