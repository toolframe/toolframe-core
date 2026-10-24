"""A provider-independent service with bounded input and output."""

from toolframe_sdk import AuthorizationSpec, MethodSpec, PluginDefinition, ServiceSpec

MESSAGE = {
    "type": "object",
    "properties": {"message": {"type": "string", "minLength": 1, "maxLength": 200}},
    "required": ["message"],
    "additionalProperties": False,
}

plugin = PluginDefinition(
    id="basic",
    authorization=AuthorizationSpec(scopes={}),
    factory="toolframe_basic.service:Echo",
    services={
        "basic.echo": ServiceSpec(
            methods={
                "echo": MethodSpec(
                    audience="internal",
                    description="Return the supplied message.",
                    input_schema=MESSAGE,
                    output_schema=MESSAGE,
                    read_only=True,
                ),
            }
        ),
    },
)
