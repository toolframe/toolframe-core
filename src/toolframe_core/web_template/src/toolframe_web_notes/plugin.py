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

from .contracts import INPUTS, OUTPUTS, SERVICE

plugin = PluginDefinition(
    id="web-notes",
    factory="toolframe_web_notes.service:Notes",
    authorization=AuthorizationSpec(
        scopes={
            "web-notes.read": ScopeSpec(title="Read notes"),
            "web-notes.write": ScopeSpec(title="Write notes"),
        }
    ),
    requires={"sql": Requirement(service="toolframe.sql")},
    services={
        SERVICE: ServiceSpec(
            methods={
                "list": MethodSpec(
                    description="List the 100 most recent shared notes, newest first.",
                    input_schema=INPUTS["list"],
                    output_schema=OUTPUTS["list"],
                    audience="business",
                    required_scopes=("web-notes.read",),
                    read_only=True,
                ),
                "create": MethodSpec(
                    description="Create a shared note. Reusing request_id with the same text returns the original note; different text raises note_conflict.",
                    input_schema=INPUTS["create"],
                    output_schema=OUTPUTS["create"],
                    audience="business",
                    required_scopes=("web-notes.write",),
                ),
                "update": MethodSpec(
                    description="Replace a shared note's text at the supplied revision. A missing or changed note raises note_conflict; success returns its new revision.",
                    input_schema=INPUTS["update"],
                    output_schema=OUTPUTS["update"],
                    audience="business",
                    required_scopes=("web-notes.write",),
                ),
            }
        )
    },
    store=StoreSchema(
        revision="v1",
        migrations=(
            Migration(
                revision="v1",
                sql="CREATE TABLE notes (id TEXT PRIMARY KEY, body TEXT NOT NULL, revision INTEGER NOT NULL, created_at TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE);",
            ),
        ),
    ),
    resources=(
        Resource(
            id="main",
            kind="web.app",
            title="Notes",
            required_scopes=("web-notes.read",),
            data={
                "contract": 1,
                "entrypoint": "toolframe_web_notes.web.app:app",
                "package": "toolframe_web_notes",
                "assets": "web/assets.json",
            },
        ),
    ),
)
