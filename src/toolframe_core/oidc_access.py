"""Public HTTP adapter for the optional shared OIDC/OAuth capability."""

import time
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from toolframe_sdk import PluginError, Principal

from .access import PASSWORD_COOKIE

BROWSER_COOKIE = "toolframe_oidc_browser"
PUBLIC_PATHS = {
    "/oauth/token",
    "/oauth/register",
    "/oauth/revoke",
    "/.well-known/oauth-authorization-server/oauth",
    "/.well-known/oauth-protected-resource/mcp",
    "/.well-known/oauth-protected-resource",
}


def cross_site_route(path, method):
    return path in PUBLIC_PATHS or (
        method == "GET"
        and (
            path == "/oauth/authorize"
            or path.startswith(
                ("/_toolframe/oidc/login/", "/_toolframe/oidc/callback/")
            )
        )
    )


def install_routes(app, host):
    async def call(method, params, *, actor=None):
        name = host.composition.access.oidc_provider
        definition = host.graph.definitions.get(name)
        if (
            not name
            or not definition
            or definition.provider_kind != "oidc-auth"
            or not host.healthy_runtime(name)
        ):
            raise PluginError("auth_unavailable", "OIDC authentication is unavailable")
        origin = host.composition.application.public_url
        if (
            not origin
            or host.composition.plugins[name].config.get("public_url", "").rstrip("/")
            != origin
        ):
            raise PluginError(
                "auth_unavailable",
                "OIDC public origin must match the application public_url",
            )
        return await host.call_service(
            name, "toolframe.oidc", method, params, principal=actor
        )

    @app.get("/_toolframe/oidc/providers")
    async def providers():
        if not host.composition.access.oidc_provider:
            return {"providers": []}
        result = await call("metadata", {})
        return {"providers": result["providers"]}

    @app.api_route(
        "/.well-known/oauth-authorization-server/oauth", methods=["GET", "OPTIONS"]
    )
    async def metadata(request: Request):
        if request.method == "OPTIONS":
            return Response(status_code=204)
        return (await call("metadata", {}))["authorization_server"]

    @app.api_route(
        "/.well-known/oauth-protected-resource/mcp", methods=["GET", "OPTIONS"]
    )
    @app.api_route("/.well-known/oauth-protected-resource", methods=["GET", "OPTIONS"])
    async def protected_resource(request: Request):
        if request.method == "OPTIONS":
            return Response(status_code=204)
        result = (await call("metadata", {}))["authorization_server"]
        return {
            "resource": host.composition.application.public_url + "/mcp",
            "authorization_servers": [result["issuer"]],
            "scopes_supported": result["scopes_supported"],
        }

    async def dispatch(request: Request):
        if request.method == "OPTIONS" and request.url.path in PUBLIC_PATHS:
            return Response(status_code=204)
        origin = host.composition.application.public_url
        if (
            request.method == "GET"
            and origin
            and request.headers.get("host", "").lower()
            != urlsplit(origin).netloc.lower()
        ):
            # Bind the browser cookie on the same host that receives the callback,
            # including when sign-in began from a local operator/LAN URL.
            target = origin + request.url.path
            if request.url.query:
                target += "?" + request.url.query
            return RedirectResponse(
                target,
                status_code=303,
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            )
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > 16384:
                return JSONResponse(
                    {
                        "error": "invalid_request",
                        "error_description": "Request exceeds 16 KiB",
                    },
                    status_code=400,
                )
        actor = None
        # Consent may reuse a user session, never loopback/operator privileges.
        if request.url.path in {"/oauth/authorize", "/oauth/consent"}:
            token = request.cookies.get(PASSWORD_COOKIE)
            if token:
                try:
                    candidate = Principal.model_validate(
                        (await host.access.auth("resolve", {"token": token}))[
                            "principal"
                        ]
                    )
                    if candidate.kind == "user":
                        actor = candidate
                except PluginError as error:
                    if error.code != "session_invalid":
                        raise
        try:
            content = body.decode("utf-8")
        except UnicodeError:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        result = await call(
            "http",
            {
                "path": request.url.path,
                "method": request.method,
                "query": request.url.query,
                "body": content,
                "headers": {
                    key: value
                    for key, value in request.headers.items()
                    if key in {"authorization", "content-type"}
                },
                "browser": request.cookies.get(BROWSER_COOKIE, ""),
            },
            actor=actor,
        )
        response = Response(
            result["body"],
            status_code=result["status"],
            headers=result.get("headers", {}),
        )
        secure = (
            urlsplit(host.composition.application.public_url).scheme == "https"
            or request.url.scheme == "https"
        )
        if result.get("browser"):
            response.set_cookie(
                BROWSER_COOKIE,
                result["browser"],
                httponly=True,
                secure=secure,
                samesite="lax",
                max_age=600,
            )
        if result.get("session"):
            response.set_cookie(
                PASSWORD_COOKIE,
                result["session"]["token"],
                httponly=True,
                secure=secure,
                samesite="lax",
                max_age=max(1, int(result["session"]["expires_at"] - time.time())),
            )
        return response

    app.add_api_route("/_toolframe/oidc/login/{provider}", dispatch, methods=["GET"])
    app.add_api_route("/_toolframe/oidc/callback/{provider}", dispatch, methods=["GET"])
    app.add_api_route("/oauth/authorize", dispatch, methods=["GET"])
    app.add_api_route("/oauth/consent", dispatch, methods=["POST"])
    for path in ("/oauth/token", "/oauth/register", "/oauth/revoke"):
        app.add_api_route(path, dispatch, methods=["POST", "OPTIONS"])
