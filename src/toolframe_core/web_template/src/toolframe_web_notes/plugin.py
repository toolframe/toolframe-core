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

SERVICE = "web-notes.notes"

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
                    audience="business",
                    required_scopes=("web-notes.read",),
                    read_only=True,
                ),
                "create": MethodSpec(
                    audience="business", required_scopes=("web-notes.write",)
                ),
                "update": MethodSpec(
                    audience="business", required_scopes=("web-notes.write",)
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
                "entrypoint": "toolframe_web_notes.webapp:app",
                "package": "toolframe_web_notes",
                "assets": "web/assets.json",
            },
        ),
    ),
)
