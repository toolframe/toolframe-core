"""Public Python API of the Notes page slice."""

from .ui.routes import create, page, update

TEMPLATES = "web/pages/notes/ui/templates"
__all__ = ["TEMPLATES", "create", "page", "update"]
