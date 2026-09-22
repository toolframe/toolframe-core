"""Stable management API and generation-gated HTTP/WebSocket entrance."""

from __future__ import annotations

import asyncio
import hmac
import json
import time
from contextlib import asynccontextmanager, suppress
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask
from starlette.requests import ClientDisconnect
from toolframe_sdk import PluginError, Principal
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .access import CONTEXT_HEADER, PASSWORD_COOKIE, operation_scopes
from .network import trusted
from .oidc_access import BROWSER_COOKIE, PUBLIC_PATHS, cross_site_route, install_routes
from .redaction import redact_toml

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


def provider_cookies(connection):
    cookies = SimpleCookie()
    for name, value in connection.cookies.items():
        if name not in {COOKIE, PASSWORD_COOKIE, BROWSER_COOKIE}:
            cookies[name] = value
    return cookies.output(header="", sep=";").strip()


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
    if code in {"authentication_required", "invalid_credentials", "session_invalid"}:
        return 401
    if code == "rate_limited":
        return 429
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
        "auth_unavailable",
        "auth_transition",
    }:
        return 503
    return 400


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
        or not host.healthy_runtime(name)
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
    async def application_operator(request, call_next):
        path = request.url.path
        identity = host.access.identity(request)
        if not trusted(
            request.headers,
            host.listen_host,
            public_url=host.composition.application.public_url,
            cross_site=cross_site_route(path, request.method),
            navigation=request.method == "GET"
            and path in {"/", "/_toolframe/auth/login"}
            and not identity.local,
        ):
            return JSONResponse(
                {
                    "error": {
                        "code": "untrusted_origin",
                        "message": "Use the application origin",
                    }
                },
                status_code=403,
            )
        request.state.identity = identity
        try:
            response = await call_next(request)
            if path in PUBLIC_PATHS:
                response.headers["access-control-allow-origin"] = "*"
                response.headers["access-control-allow-methods"] = "GET, POST, OPTIONS"
                response.headers["access-control-allow-headers"] = (
                    "Authorization, Content-Type"
                )
                response.headers["cache-control"] = "no-store"
            if path.startswith("/_toolframe/"):
                response.headers["cache-control"] = "no-store"
                response.headers["referrer-policy"] = "no-referrer"
            return response
        except PluginError as error:
            return JSONResponse(
                {"error": host.redact(error.as_dict())}, status_code=error_status(error)
            )

    @app.exception_handler(PluginError)
    async def plugin_error(request, error):
        return JSONResponse(
            {"error": host.redact(error.as_dict())}, status_code=error_status(error)
        )

    async def require(request, action, params=None, *, activity=False):
        return await host.access.admit(
            request.state.identity,
            operation_scopes(action, params),
            effective=False,
            activity=activity,
        )

    async def bounded_body(request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 16384:
                raise PluginError("invalid_request", "Auth request exceeds 16 KiB")
        try:
            if request.headers.get("content-type", "").startswith("application/json"):
                result = json.loads(body)
            else:
                result = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            if not isinstance(result, dict):
                raise TypeError()
            return result
        except ValueError, UnicodeError, TypeError:
            raise PluginError("invalid_request", "Supply an object or form") from None

    @app.get("/healthz")
    async def health():
        return {"core": "ready", "host_run": host.host_run}

    install_routes(app, host)

    @app.get("/_toolframe/auth/context")
    async def access_context(request: Request):
        return await host.access.presentation(request.state.identity)

    @app.api_route("/_toolframe/auth/login", methods=["GET", "POST"])
    async def password_login(request: Request):
        if request.method == "POST":
            data = await bounded_body(request)
            result = await host.access.login(
                data.get("password"),
                request.client.host if request.client else "unknown",
            )
            response = (
                JSONResponse({"status": "signed_in"})
                if "application/json" in request.headers.get("accept", "")
                else RedirectResponse("/", status_code=303)
            )
            response.set_cookie(
                PASSWORD_COOKIE,
                result["token"],
                httponly=True,
                samesite="strict",
                secure=host.composition.application.public_url.startswith("https://")
                or request.url.scheme == "https",
                max_age=max(1, int(result["expires_at"] - time.time())),
            )
            return response
        name = host.composition.access.dashboard_auth
        try:
            result = await host.call_service(
                name, "dashboard-auth.presentation", "login", {}
            )
            if not isinstance(result.get("html"), str):
                raise TypeError("Invalid login presentation")
            return HTMLResponse(result["html"])
        except Exception:  # noqa: BLE001 - an unavailable login integration selects fallback
            if name not in host.transitioning and not host.starting:
                host.access.mark_failed(name)
            return HTMLResponse(
                '<!doctype html><title>Toolframe</title><p>Login is unavailable.</p><a href="/">Open Dashboard</a>',
                status_code=503,
            )

    @app.post("/_toolframe/auth/logout")
    async def logout(request: Request):
        token = request.cookies.get(PASSWORD_COOKIE)
        if token:
            with suppress(PluginError):
                await host.access.auth("logout", {"token": token})
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(PASSWORD_COOKIE)
        response.delete_cookie(COOKIE)
        return response

    @app.post("/_toolframe/v1/auth/{action}")
    async def auth_operation(action: str, request: Request):
        return await host.access.manage(
            request.state.identity, action, await bounded_body(request)
        )

    @app.post("/_toolframe/v1/mcp-auth/{action}")
    async def mcp_auth_operation(action: str, request: Request):
        return await host.mcp_access.manage(
            request.state.identity, action, await bounded_body(request)
        )

    @app.api_route("/_toolframe/session", methods=["GET", "POST"])
    async def session(request: Request):
        token = request.query_params.get("token", "")
        if request.method == "POST":
            data = await request.body()
            if len(data) > 4096:
                return JSONResponse({"error": "Request too large"}, status_code=413)
            try:
                token = parse_qs(data.decode()).get("token", [""])[0]
            except UnicodeError:
                return JSONResponse(
                    {"error": "Invalid request encoding"}, status_code=422
                )
        if token:
            if not hmac.compare_digest(token.encode(), host.token.encode()):
                return JSONResponse(
                    {"error": "Invalid management token"}, status_code=401
                )
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(
                COOKIE,
                token,
                httponly=True,
                samesite="strict",
                secure=host.composition.application.public_url.startswith("https://")
                or request.url.scheme == "https",
            )
            response.headers["referrer-policy"] = "no-referrer"
            response.headers["cache-control"] = "no-store"
            return response
        return HTMLResponse(
            '<!doctype html><title>Toolframe sign in</title><h1>Toolframe</h1><form method="post">'
            '<label>Management token <input type="password" name="token" required></label>'
            "<button>Open Dashboard</button></form><p>On the server, run <code>toolframe open</code> for a local sign-in link. "
            "For LAN access, enter the token from <code>APP/.toolframe/management.token</code>.</p>"
        )

    @app.get("/_toolframe/v1/status")
    async def status(request: Request):
        await require(request, "status")
        return host.status()

    @app.get("/_toolframe/v1/readiness")
    async def readiness(request: Request):
        await require(request, "readiness")
        report = host.status()
        return JSONResponse(report, status_code=200 if report["ready"] else 503)

    @app.get("/_toolframe/v1/composition")
    async def composition(request: Request):
        await require(request, "composition")
        try:
            text = redact_toml(host.config.path.read_text(), host.redact)
        except ValueError as error:
            raise PluginError(
                "configuration_invalid", "Repair invalid TOML before exporting"
            ) from error
        return {"toml": text, "revision": host.config.revision, "secrets": "redacted"}

    @app.get("/_toolframe/v1/operations")
    async def operations(
        request: Request,
        page: int | None = None,
        page_size: int = 10,
        through: int | None = None,
        target: str | None = None,
        kind: str | None = None,
        status: str | None = None,
    ):
        await require(request, "operations")
        if request.query_params:
            return host.redact(
                host.state.operation_page(
                    {
                        "page": page if page is not None else 1,
                        "page_size": page_size,
                        "through": through,
                        "target": target,
                        "kind": kind,
                        "status": status,
                    }
                )
            )
        # Preserve the existing bounded list response for clients without queries.
        return host.redact(host.state.operations())

    @app.get("/_toolframe/v1/operations/{identifier}")
    async def operation(identifier: str, request: Request):
        await require(request, "operation")
        return host.redact(host.state.operation(identifier))

    @app.post("/_toolframe/v1/operations/{identifier}/cancel")
    async def cancel(identifier: str, request: Request):
        current = host.state.operation(identifier)
        await require(request, current["kind"], current["request"], activity=True)
        if current["status"] not in ("queued", "running"):
            return {"id": identifier, "requested": False, "status": current["status"]}
        host.cancellations.add(identifier)
        host.changed.set()
        return {"id": identifier, "requested": True, "phase": current["phase"]}

    @app.post("/_toolframe/v1/operations", status_code=202)
    async def submit(request: Request, body: OperationRequest):
        await require(request, body.action, body.params)
        return host.redact(
            host.submit(
                body.action,
                body.params,
                idempotency=request.headers.get("idempotency-key"),
                identity=request.state.identity,
            )
        )

    @app.post("/_toolframe/v1/plugins/{identifier}/{action}", status_code=202)
    async def plugin_operation(
        identifier: str, action: str, request: Request, body: dict
    ):
        await require(request, action, {**body, "id": identifier})
        return host.redact(
            host.submit(
                action,
                {**body, "id": identifier},
                idempotency=request.headers.get("idempotency-key"),
                identity=request.state.identity,
            )
        )

    @app.get("/_toolframe/v1/plugins/{identifier}/logs")
    async def logs(identifier: str, request: Request):
        await require(request, "logs")
        runtime = host.runtimes.get(identifier)
        return host.redact(
            {
                "plugin_id": identifier,
                "lines": list(runtime.worker.logs) if runtime else [],
            }
        )

    @app.post("/_toolframe/v1/services/{owner}/{service}/{method}")
    async def service_call(owner: str, service: str, method: str, request: Request):
        definition = host.graph.definitions.get(owner)
        spec = (
            definition.services.get(service).methods.get(method)
            if definition and service in definition.services
            else None
        )
        if spec is None or spec.audience == "internal":
            raise PluginError("forbidden", "This service is not a visitor-facing API")
        actor = await host.access.admit(
            request.state.identity, spec.required_scopes, activity=True
        )
        return await host.call_service(
            owner, service, method, await request.json(), principal=actor
        )

    @app.post("/api/{owner}/{service}/{method}")
    async def public_business_call(
        owner: str, service: str, method: str, request: Request
    ):
        definition = host.graph.definitions.get(owner)
        spec = (
            definition.services.get(service).methods.get(method)
            if definition and service in definition.services
            else None
        )
        if spec is None or not spec.public_api or spec.audience != "business":
            raise PluginError("forbidden", "This method has no public API declaration")
        return await host.call_service(
            owner,
            service,
            method,
            await request.json(),
            principal=Principal(kind="public", all_business=True),
        )

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
        if kind == "dashboard":
            try:
                await host.access.admit(request.state.identity)
            except PluginError as error:
                if error.code == "authentication_required":
                    return RedirectResponse("/_toolframe/auth/login", status_code=303)
                raise
        mcp_reference = None
        if kind == "mcp":
            try:
                mcp_reference = await host.mcp_access.register(runtime, request.headers)
            except PluginError as error:
                status = error_status(error)
                challenge = (
                    'Bearer error="invalid_token"'
                    if error.code == "invalid_credentials"
                    else "Bearer"
                )
                if (
                    host.composition.access.oidc_provider
                    and host.composition.application.public_url
                ):
                    separator = ", " if challenge != "Bearer" else " "
                    challenge += (
                        separator
                        + 'resource_metadata="'
                        + host.composition.application.public_url
                        + '/.well-known/oauth-protected-resource/mcp"'
                    )
                return JSONResponse(
                    {"error": error.as_dict()},
                    status_code=status,
                    headers={
                        "Cache-Control": "no-store",
                        **({"WWW-Authenticate": challenge} if status == 401 else {}),
                        **({"Retry-After": "1"} if status == 503 else {}),
                    },
                )
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in HOP_HEADERS
            and key.lower() not in {"authorization", "cookie"}
            and not key.lower().startswith("x-toolframe-")
        }
        if "origin" in headers:
            headers["origin"] = address
        if mcp_reference:
            headers["authorization"] = "Bearer " + mcp_reference
        if cookies := provider_cookies(request):
            headers["cookie"] = cookies
        url = f"{address}/{path}"
        if request.url.query:
            url += "?" + request.url.query
        client = httpx.AsyncClient(timeout=None, trust_env=False)
        try:
            upstream_request = client.build_request(
                request.method, url, headers=headers, content=request.stream()
            )
            upstream = await client.send(upstream_request, stream=True)
        except ClientDisconnect:
            host.mcp_access.release(mcp_reference)
            await client.aclose()
            # The caller left before its upload finished; this is not a server error.
            return Response(status_code=499)
        except BaseException:
            host.mcp_access.release(mcp_reference)
            await client.aclose()
            raise

        async def close():
            host.mcp_access.release(mcp_reference)
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
        if not trusted(
            downstream.headers,
            host.listen_host,
            public_url=host.composition.application.public_url,
        ) or path.startswith("_toolframe/"):
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
        identity = host.access.identity(downstream)
        try:
            initial_actor = await host.access.admit(identity)
            reference = host.access.register(identity)
        except PluginError:
            await downstream.close(code=1008)
            return
        initial_mode = host.access.mode()
        url = address.replace("http://", "ws://", 1) + "/" + path
        if downstream.url.query:
            url += "?" + downstream.url.query
        protocols = downstream.scope.get("subprotocols", [])
        headers = {
            key: value
            for key, value in downstream.headers.items()
            if key.lower() not in HOP_HEADERS
            and not key.lower().startswith("sec-websocket-")
            and key.lower() not in ("origin", "authorization", "cookie")
            and not key.lower().startswith("x-toolframe-")
        }
        headers[CONTEXT_HEADER] = reference
        if cookies := provider_cookies(downstream):
            headers["cookie"] = cookies
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

                async def session_validity():
                    while True:
                        await asyncio.sleep(1)
                        try:
                            await host.access.admit(identity)
                        except PluginError:
                            return
                        if (
                            not initial_actor.unrestricted
                            and host.access.mode() != initial_mode
                        ):
                            return

                pumps = [
                    asyncio.create_task(incoming()),
                    asyncio.create_task(outgoing()),
                    asyncio.create_task(session_validity()),
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
            host.access.contexts.pop(reference, None)
            host.gateway_sessions.get(runtime.name, set()).discard(task)
            with suppress(RuntimeError):
                await downstream.close(code=1012)

    return app
