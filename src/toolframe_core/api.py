"""Stable management API and generation-gated HTTP/WebSocket entrance."""

from __future__ import annotations

import asyncio
import hmac
from contextlib import asynccontextmanager, suppress
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask
from toolframe_sdk import PluginError
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

COOKIE = "toolframe_session"
HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


class OperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    params: dict = Field(default_factory=dict)


def error_status(error):
    code = error.code
    if code in {
        "stale_plan",
        "stale_revision",
        "idempotency_conflict",
        "already_installed",
        "mutation_conflict",
    }:
        return 409
    if code in {"plugin_missing", "operation_missing", "candidate_missing"}:
        return 404
    if code == "forbidden":
        return 403
    if code in {
        "invalid_request",
        "action_unknown",
        "configuration_invalid",
        "config_invalid",
    }:
        return 422
    if code in {
        "host_closing",
        "plugin_reloading",
        "dependency_unavailable",
        "presentation_unavailable",
        "peer_closed",
        "cleanup_failed",
    }:
        return 503
    return 400


def trusted(headers):
    authority = headers.get("host", "")
    hostname = urlsplit(f"http://{authority}").hostname
    if hostname not in ("127.0.0.1", "localhost", "::1"):
        return False
    origin = headers.get("origin")
    if origin and origin.rstrip("/") not in (
        f"http://{authority}",
        f"https://{authority}",
    ):
        return False
    return headers.get("sec-fetch-site") != "cross-site"


def authenticated(connection, host):
    authorization = connection.headers.get("authorization", "")
    token = (
        authorization.removeprefix("Bearer ")
        if authorization.startswith("Bearer ")
        else connection.cookies.get(COOKIE, "")
    )
    return hmac.compare_digest(token, host.token)


def provider_for(host, path):
    kind = (
        "mcp"
        if path == "mcp" or path.startswith("mcp/")
        else "llms"
        if path == "llms.txt" or path.startswith("llms/")
        else "dashboard"
    )
    name = host.graph.providers.get(kind)
    runtime = host.runtimes.get(name)
    if (
        not runtime
        or not runtime.admitted
        or name in host.gated
        or runtime.epoch != host.epoch
        or host.publication.get(kind, {}).get("state") != "available"
    ):
        raise PluginError("presentation_unavailable", f"{kind} entrance is unavailable")
    address = runtime.health.get("http_url")
    if not address or urlsplit(address).hostname != "127.0.0.1":
        raise PluginError(
            "provider_address_invalid",
            "Provider must report a private loopback HTTP address",
        )
    return kind, runtime, address


def create_app(host, *, manage_lifespan=True):
    @asynccontextmanager
    async def lifespan(app):
        if manage_lifespan:
            await host.start(wait=False)
        try:
            yield
        finally:
            if manage_lifespan:
                await host.close()

    app = FastAPI(
        title="Toolframe core",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def local_operator(request, call_next):
        if not trusted(request.headers):
            return JSONResponse(
                {
                    "error": {
                        "code": "untrusted_origin",
                        "message": "Use the local application origin",
                    }
                },
                status_code=403,
            )
        path = request.url.path
        if (
            path.startswith("/_toolframe/")
            and path != "/_toolframe/session"
            and not authenticated(request, host)
        ):
            return JSONResponse(
                {
                    "error": {
                        "code": "authentication_required",
                        "message": "A local management token is required",
                    }
                },
                status_code=401,
            )
        try:
            return await call_next(request)
        except PluginError as error:
            return JSONResponse(
                {"error": error.as_dict()}, status_code=error_status(error)
            )

    @app.exception_handler(PluginError)
    async def plugin_error(request, error):
        return JSONResponse({"error": error.as_dict()}, status_code=error_status(error))

    @app.get("/healthz")
    async def health():
        return {"core": "ready", "host_run": host.host_run}

    @app.api_route("/_toolframe/session", methods=["GET", "POST"])
    async def session(request: Request):
        token = request.query_params.get("token", "")
        if request.method == "POST":
            data = await request.body()
            if len(data) > 4096:
                return JSONResponse({"error": "Request too large"}, status_code=413)
            token = parse_qs(data.decode()).get("token", [""])[0]
        if token:
            if not hmac.compare_digest(token, host.token):
                return JSONResponse(
                    {"error": "Invalid management token"}, status_code=401
                )
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(
                COOKIE, token, httponly=True, samesite="strict", secure=False
            )
            response.headers["referrer-policy"] = "no-referrer"
            response.headers["cache-control"] = "no-store"
            return response
        return HTMLResponse(
            '<!doctype html><title>Toolframe sign in</title><h1>Toolframe</h1><form method="post">'
            '<label>Local management token <input type="password" name="token" required></label>'
            "<button>Open Dashboard</button></form><p>Run <code>toolframe open</code> for a local sign-in link.</p>"
        )

    @app.get("/_toolframe/v1/status")
    async def status():
        return host.status()

    @app.get("/_toolframe/v1/composition")
    async def composition():
        return {"toml": host.config.path.read_text(), "revision": host.config.revision}

    @app.get("/_toolframe/v1/operations")
    async def operations():
        return host.state.operations()

    @app.get("/_toolframe/v1/operations/{identifier}")
    async def operation(identifier: str):
        return host.state.operation(identifier)

    @app.post("/_toolframe/v1/operations/{identifier}/cancel")
    async def cancel(identifier: str):
        current = host.state.operation(identifier)
        if current["status"] not in ("queued", "running"):
            return {"id": identifier, "requested": False, "status": current["status"]}
        host.cancellations.add(identifier)
        host.changed.set()
        return {"id": identifier, "requested": True, "phase": current["phase"]}

    @app.post("/_toolframe/v1/operations", status_code=202)
    async def submit(request: Request, body: OperationRequest):
        return host.submit(
            body.action,
            body.params,
            idempotency=request.headers.get("idempotency-key"),
        )

    @app.post("/_toolframe/v1/plugins/{identifier}/{action}", status_code=202)
    async def plugin_operation(
        identifier: str, action: str, request: Request, body: dict
    ):
        return host.submit(
            action,
            {**body, "id": identifier},
            idempotency=request.headers.get("idempotency-key"),
        )

    @app.get("/_toolframe/v1/plugins/{identifier}/logs")
    async def logs(identifier: str):
        runtime = host.runtimes.get(identifier)
        return {
            "plugin_id": identifier,
            "lines": list(runtime.worker.logs) if runtime else [],
        }

    @app.post("/_toolframe/v1/services/{owner}/{service}/{method}")
    async def service_call(owner: str, service: str, method: str, request: Request):
        if service == "toolframe.storage.admin":
            raise PluginError("forbidden", "Use reviewed migration operations")
        return await host.call_service(owner, service, method, await request.json())

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    )
    async def gateway(path: str, request: Request):
        if path.startswith("_toolframe/"):
            return JSONResponse({"error": "Unknown management route"}, status_code=404)
        if not path and "dashboard" not in host.graph.providers:
            return JSONResponse(
                {
                    "name": host.composition.application.name,
                    "core": "ready",
                    "management": "/_toolframe/v1/status",
                    "dashboard": "not_installed",
                }
            )
        try:
            kind, runtime, address = provider_for(host, path)
        except PluginError as error:
            return JSONResponse(
                {"error": error.as_dict()},
                status_code=503,
                headers={"Retry-After": "1"},
            )
        if kind == "dashboard" and not authenticated(request, host):
            return RedirectResponse("/_toolframe/session", status_code=303)
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in HOP_HEADERS and key.lower() != "authorization"
        }
        if "origin" in headers:
            headers["origin"] = address
        url = f"{address}/{path}"
        if request.url.query:
            url += "?" + request.url.query
        client = httpx.AsyncClient(timeout=None, trust_env=False)
        try:
            upstream_request = client.build_request(
                request.method, url, headers=headers, content=request.stream()
            )
            upstream = await client.send(upstream_request, stream=True)
        except Exception:
            await client.aclose()
            raise

        async def close():
            await upstream.aclose()
            await client.aclose()

        async def stream():
            task = asyncio.current_task()
            host.gateway_sessions.setdefault(runtime.name, set()).add(task)
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                host.gateway_sessions.get(runtime.name, set()).discard(task)
                await close()

        response = StreamingResponse(
            stream(), status_code=upstream.status_code, background=BackgroundTask(close)
        )
        response.raw_headers = [
            (key, value)
            for key, value in upstream.headers.raw
            if key.decode().lower() not in HOP_HEADERS
        ]
        return response

    @app.websocket("/{path:path}")
    async def websocket(path: str, downstream: WebSocket):
        if (
            not trusted(downstream.headers)
            or not authenticated(downstream, host)
            or path.startswith("_toolframe/")
        ):
            await downstream.close(code=1008)
            return
        try:
            kind, runtime, address = provider_for(host, path)
        except PluginError:
            await downstream.close(code=1013)
            return
        if kind != "dashboard":
            await downstream.close(code=1008)
            return
        url = address.replace("http://", "ws://", 1) + "/" + path
        if downstream.url.query:
            url += "?" + downstream.url.query
        protocols = downstream.scope.get("subprotocols", [])
        headers = {
            key: value
            for key, value in downstream.headers.items()
            if key.lower() not in HOP_HEADERS
            and not key.lower().startswith("sec-websocket-")
            and key.lower() not in ("origin", "authorization")
        }
        task = asyncio.current_task()
        host.gateway_sessions.setdefault(runtime.name, set()).add(task)
        try:
            async with connect(
                url,
                origin=address,
                subprotocols=protocols or None,
                additional_headers=headers,
                max_size=200 * 1024 * 1024,
                proxy=None,
            ) as upstream:
                await downstream.accept(subprotocol=upstream.subprotocol)

                async def incoming():
                    while True:
                        event = await downstream.receive()
                        if event["type"] == "websocket.disconnect":
                            return
                        await upstream.send(
                            event.get("text")
                            if event.get("text") is not None
                            else event["bytes"]
                        )

                async def outgoing():
                    async for message in upstream:
                        if isinstance(message, str):
                            await downstream.send_text(message)
                        else:
                            await downstream.send_bytes(message)

                pumps = [
                    asyncio.create_task(incoming()),
                    asyncio.create_task(outgoing()),
                ]
                try:
                    done, _ = await asyncio.wait(
                        pumps, return_when=asyncio.FIRST_COMPLETED
                    )
                    for finished in done:
                        await finished
                finally:
                    for pump in pumps:
                        pump.cancel()
                    await asyncio.gather(*pumps, return_exceptions=True)
        except ConnectionClosed, asyncio.CancelledError, OSError:
            pass
        finally:
            host.gateway_sessions.get(runtime.name, set()).discard(task)
            with suppress(RuntimeError):
                await downstream.close(code=1012)

    return app
