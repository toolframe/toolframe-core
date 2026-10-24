"""Transport adapter; durable rules and data remain in the business service."""

from jsonschema import ValidationError
from toolframe_sdk import PluginError, validate

from .....contracts import INPUTS, SERVICE


async def call(ctx, method, payload=None):
    payload = {} if payload is None else payload
    try:
        validate(INPUTS[method], payload)
    except ValidationError:
        raise PluginError(
            "note_invalid", "Check the note text, request ID and revision."
        ) from None
    return await ctx.call(SERVICE + "@1/" + method, payload)
