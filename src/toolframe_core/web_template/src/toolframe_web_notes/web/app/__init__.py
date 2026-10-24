"""Application composition: routes, entrypoint and global styles."""

from toolframe_sdk import WebApp, WebRoute

from ..pages.notes import TEMPLATES, create, page, update


def app():
    return WebApp(
        routes=(
            WebRoute("index", "/", ("GET",), page),
            WebRoute("create", "/", ("POST",), create, ("web-notes.write",)),
            WebRoute("update", "/notes/{id}", ("POST",), update, ("web-notes.write",)),
        ),
        templates=TEMPLATES,
    )
