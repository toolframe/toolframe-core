from uuid import uuid4

from toolframe_sdk import PluginError
from toolframe_web import json, redirect, render

from ..api import call
from ..model import input_data, row_revision


async def page(request, ctx, *, error="", value="", status=200, request_id=None):
    notes = (await call(ctx, "list"))["notes"]
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
    data, _is_json = await input_data(request)
    try:
        note = await call(
            ctx,
            "create",
            {"body": data.get("body"), "request_id": data.get("request_id")},
        )
    except PluginError as error:
        if error.code not in {"note_invalid", "note_conflict"}:
            raise
        return await page(
            request,
            ctx,
            error=error.message,
            value=data.get("body", "") if isinstance(data.get("body", ""), str) else "",
            status=409 if error.code == "note_conflict" else 422,
            request_id=data.get("request_id")
            if isinstance(data.get("request_id"), str)
            else None,
        )
    if "application/json" in request.headers.get("accept", ""):
        return json(note, status=201)
    return (
        await page(request, ctx)
        if request.headers.get("hx-request") == "true"
        else redirect(ctx.url("index"))
    )


async def update(request, ctx):
    data, is_json = await input_data(request)
    try:
        revision = row_revision(data, is_json)
        await call(
            ctx,
            "update",
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
