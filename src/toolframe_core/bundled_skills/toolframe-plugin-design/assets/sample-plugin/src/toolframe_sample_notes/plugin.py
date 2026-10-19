"""Definitions import without Streamlit or provider implementation dependencies."""

from importlib.resources import files

from toolframe_sdk import (
    AuthorizationSpec,
    MethodSpec,
    Migration,
    PluginDefinition,
    Requirement,
    Resource,
    ScopeSpec,
    ServiceSpec,
    StoreSchema,
)

SERVICE = "sample-notes.notes"
PACKAGE = "toolframe_sample_notes"
NOTE = {
    "type": "object",
    "properties": {
        "id": {"type": "integer", "minimum": 1},
        "body": {"type": "string", "minLength": 1, "maxLength": 1000},
        "created_at": {"type": "string"},
    },
    "required": ["id", "body", "created_at"],
    "additionalProperties": False,
}
LIST_INPUT = {
    "type": "object",
    "properties": {
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
    },
    "additionalProperties": False,
}
LIST_OUTPUT = {
    "type": "object",
    "properties": {
        "notes": {"type": "array", "items": NOTE},
        "count": {"type": "integer", "minimum": 0},
    },
    "required": ["notes", "count"],
    "additionalProperties": False,
}

plugin = PluginDefinition(
    id="sample-notes",
    factory="toolframe_sample_notes.service:Notes",
    authorization=AuthorizationSpec(
        scopes={
            "sample-notes.read": ScopeSpec(title="Read notes"),
            "sample-notes.write": ScopeSpec(title="Capture notes"),
        }
    ),
    requires={"sql": Requirement(service="toolframe.sql")},
    services={
        SERVICE: ServiceSpec(
            methods={
                "capture": MethodSpec(
                    audience="business",
                    required_scopes=("sample-notes.write",),
                    description="Capture a new note.",
                    input_schema={
                        "type": "object",
                        "properties": {"body": NOTE["properties"]["body"]},
                        "required": ["body"],
                        "additionalProperties": False,
                    },
                    output_schema=NOTE,
                ),
                "list_notes": MethodSpec(
                    audience="business",
                    required_scopes=("sample-notes.read",),
                    public_api=True,
                    description="Read the newest notes.",
                    input_schema=LIST_INPUT,
                    output_schema=LIST_OUTPUT,
                    read_only=True,
                ),
            }
        )
    },
    store=StoreSchema(
        revision="v1",
        migrations=(
            Migration(
                revision="v1",
                sql=files(PACKAGE).joinpath("migrations/0001_init.sql").read_text(),
            ),
        ),
    ),
    resources=(
        Resource(
            id="notes",
            kind="dashboard.page",
            title="Notes",
            required_scopes=("sample-notes.read",),
            data={"entrypoint": "toolframe_sample_notes.page:render", "order": 10},
        ),
        Resource(
            id="list_notes",
            kind="mcp.tool",
            title="List notes",
            data={
                "service": SERVICE,
                "method": "list_notes",
                "description": "Read the newest captured notes.",
                "input_schema": LIST_INPUT,
                "output_schema": LIST_OUTPUT,
            },
        ),
        Resource(
            id="usage",
            kind="llms.document",
            title="Notes",
            data={"package": PACKAGE, "path": "docs/usage.md"},
        ),
    ),
)
