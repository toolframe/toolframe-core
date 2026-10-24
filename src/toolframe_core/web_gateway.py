"""Bounded Web HTTP forwarding with Core-owned identity and path selection."""

from __future__ import annotations

import asyncio
import html
import secrets
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlencode

import httpx
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.requests import ClientDisconnect
from toolframe_sdk import PluginError

from .access import OPERATOR_COOKIE, PASSWORD_COOKIE
from .oidc_access import BROWSER_COOKIE


async def entrance(host, request):
    links = await host.web_access.links(request.state.identity)
    items = "".join(
        f'<li><a href="{html.escape(url, quote=True)}">{html.escape(name)}</a></li>'
        for name, url in links.items()
    )
    return HTMLResponse(
        f'<!doctype html><meta charset="utf-8"><title>Toolframe</title><h1>Toolframe</h1><ul>{items}</ul><a href="/_toolframe/auth/login">Sign in</a>',
        headers={
            "Cache-Control": "private, no-store",
            "Content-Security-Policy": "default-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        },
    )


async def forward(host, request, mount, runtime, address):
    from .api import HOP_HEADERS, error_status

    request_id = secrets.token_hex(12)
    raw = request.scope.get("raw_path", b"").lower()
    path = request.url.path
    if (
        any(value in raw for value in (b"%2f", b"%5c", b"%2e", b"%00", b"%25"))
        or "//" in path
        or "\\" in path
        or any(ord(c) < 32 for c in path)
        or any(p in {".", ".."} for p in path.split("/"))
    ):
        return JSONResponse(
            {
                "error": {
                    "code": "invalid_request",
                    "message": "Use a canonical Web path",
                }
            },
            status_code=400,
        )
    if mount.prefix != "/" and path == mount.prefix:
        if request.method in {"GET", "HEAD"}:
            return RedirectResponse(
                path + "/" + ("?" + request.url.query if request.url.query else ""),
                status_code=308,
            )
        return JSONResponse(
            {
                "error": {
                    "code": "invalid_request",
                    "message": "Use the canonical app URL ending in /",
                }
            },
            status_code=400,
        )
    _, owner, identifier = mount.target.split(".")
    generations = {name: item.generation for name, item in host.runtimes.items()}
    resource = next(
        (
            r
            for r in host.graph.resources(generations)
            if r.owner == owner
            and r.resource.id == identifier
            and r.resource.kind == "web.app"
        ),
        None,
    )
    if (
        resource is None
        or not host.healthy_runtime(owner)
        or not host.runtimes[owner].admitted
        or owner in host.gated
    ):
        raise PluginError("presentation_unavailable", "Selected Web app is unavailable")
    reference = host.web_access.register(
        request.state.identity,
        runtime,
        resource,
        request.method,
        mount.relative(path),
        mount,
        host.gateway,
    )
    task = asyncio.current_task()
    host.gateway_sessions.setdefault(runtime.name, set()).add(task)
    try:
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower()
            not in HOP_HEADERS | {"authorization", "cookie", "forwarded", "origin"}
            and not key.lower().startswith(("x-toolframe-", "x-forwarded-"))
        }
        headers.update(
            {
                "x-toolframe-context": reference,
                "x-toolframe-resource": resource.key,
                "x-toolframe-request-id": request_id,
            }
        )
        body = bytearray()
        async with asyncio.timeout(host.composition.web.request_timeout):
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > host.composition.web.max_body_bytes:
                    return JSONResponse(
                        {
                            "error": {
                                "code": "request_too_large",
                                "message": "Web request exceeds the configured body limit",
                            }
                        },
                        status_code=413,
                    )
            url = address + mount.relative(path)
            if request.url.query:
                url += "?" + request.url.query
            async with (
                httpx.AsyncClient(
                    timeout=host.composition.web.request_timeout, trust_env=False
                ) as client,
                client.stream(
                    request.method, url, headers=headers, content=bytes(body)
                ) as upstream,
            ):
                response_body = bytearray()
                async for chunk in upstream.aiter_raw():
                    response_body.extend(chunk)
                    if len(response_body) > 32 * 1024 * 1024:
                        raise PluginError(
                            "response_too_large", "Web response exceeds 32 MiB"
                        )
                if (
                    upstream.status_code == 401
                    and request.method in {"GET", "HEAD"}
                    and "text/html" in request.headers.get("accept", "")
                    and not request.headers.get("hx-request")
                    and request.headers.get("sec-fetch-mode", "navigate") == "navigate"
                ):
                    destination = request.url.path + (
                        "?" + request.url.query if request.url.query else ""
                    )
                    return RedirectResponse(
                        "/_toolframe/auth/login?"
                        + urlencode({"return_to": destination}),
                        status_code=303,
                        headers={"Cache-Control": "no-store"},
                    )
                response = Response(
                    bytes(response_body), status_code=upstream.status_code
                )
                clean = []
                for key, value in upstream.headers.raw:
                    name = key.decode().lower()
                    if (
                        name in HOP_HEADERS
                        or name
                        in {"service-worker-allowed", "access-control-allow-origin"}
                        or name.startswith("x-toolframe-")
                    ):
                        continue
                    if name == "set-cookie":
                        cookies = SimpleCookie()
                        try:
                            cookies.load(value.decode("latin-1"))
                        except CookieError:
                            continue
                        if not cookies or set(cookies) & {
                            OPERATOR_COOKIE,
                            PASSWORD_COOKIE,
                            BROWSER_COOKIE,
                        }:
                            continue
                    clean.append((key, value))
                response.raw_headers = clean + [
                    (b"x-request-id", request_id.encode()),
                    (b"x-content-type-options", b"nosniff"),
                ]
                if not mount.relative(path).startswith("/_tf/assets/"):
                    response.headers["cache-control"] = "private, no-store"
                return response
    except ClientDisconnect:
        return Response(status_code=499)
    except TimeoutError, httpx.TimeoutException:
        return JSONResponse(
            {
                "error": {"code": "web_timeout", "message": "Web request timed out"},
                "request_id": request_id,
            },
            status_code=504,
        )
    except httpx.HTTPError:
        return JSONResponse(
            {
                "error": {
                    "code": "presentation_unavailable",
                    "message": "Web provider is unavailable",
                },
                "request_id": request_id,
            },
            status_code=503,
        )
    except PluginError as error:
        return JSONResponse(
            {"error": error.as_dict(), "request_id": request_id},
            status_code=error_status(error),
        )
    finally:
        host.web_access.release(reference)
        host.gateway_sessions.get(runtime.name, set()).discard(task)
