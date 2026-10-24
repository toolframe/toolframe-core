from uuid import uuid4

from toolframe_sdk import PluginError, WebApp, WebRoute
from toolframe_web import json, redirect, render

from .plugin import SERVICE


async def page(request, ctx, *, error="", value="", status=200, request_id=None):
    notes = (await ctx.call(SERVICE + "@1/list"))["notes"]
    if "application/json" in request.headers.get("accept", ""):
        return json(
            {"notes": notes, "revision": ctx.revision, "csrf": ctx.csrf_token},
            status=status,
        )
    template = (
        "notes.html" if request.headers.get("hx-request") == "true" else "index.html"
    )
    return render(
        ctx,
        template,
        notes=notes,
        error=error,
        value=value,
        request_id=request_id or uuid4().hex,
        can_write="web-notes.write" in ctx.presentation["allowed_scopes"],
        status=status,
    )


async def create(request, ctx):
    data = (
        request.json()
        if "application/json" in request.headers.get("content-type", "")
        else await request.form()
    )
    try:
        note = await ctx.call(
            SERVICE + "@1/create",
            {"body": data.get("body"), "request_id": data.get("request_id")},
        )
    except PluginError as error:
        if error.code not in {"note_invalid", "note_conflict"}:
            raise
        return await page(
            request,
            ctx,
            error=error.message,
            value=data.get("body", ""),
            status=409 if error.code == "note_conflict" else 422,
            request_id=data.get("request_id"),
        )
    if "application/json" in request.headers.get("accept", ""):
        return json(note, status=201)
    return (
        await page(request, ctx)
        if request.headers.get("hx-request") == "true"
        else redirect(ctx.url("index"))
    )


async def update(request, ctx):
    data = (
        request.json()
        if "application/json" in request.headers.get("content-type", "")
        else await request.form()
    )
    try:
        revision = int(data.get("revision", 0))
        await ctx.call(
            SERVICE + "@1/update",
            {
                "id": request.path_params["id"],
                "body": data.get("body"),
                "revision": revision,
            },
        )
    except (ValueError, PluginError) as error:
        if isinstance(error, PluginError) and error.code not in {
            "note_invalid",
            "note_conflict",
        }:
            raise
        return await page(
            request,
            ctx,
            error=str(error),
            status=409
            if isinstance(error, PluginError) and error.code == "note_conflict"
            else 422,
        )
    return (
        await page(request, ctx)
        if request.headers.get("hx-request") == "true"
        else redirect(ctx.url("index"))
    )


def app():
    return WebApp(
        routes=(
            WebRoute("index", "/", ("GET",), page),
            WebRoute("create", "/", ("POST",), create, ("web-notes.write",)),
            WebRoute("update", "/notes/{id}", ("POST",), update, ("web-notes.write",)),
        )
    )
