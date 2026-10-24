"""HTTP input normalization for this page, without business mutations."""

from collections.abc import Mapping

from toolframe_sdk import PluginError


async def input_data(request):
    is_json = (
        request.headers.get("content-type", "").split(";", 1)[0] == "application/json"
    )
    data = request.json() if is_json else await request.form()
    if not isinstance(data, Mapping):
        raise PluginError("invalid_request", "Supply a JSON object or form fields")
    return data, is_json


def row_revision(data, is_json):
    value = data.get("revision")
    # JSON types must reach the contract unchanged; only forms encode numbers as text.
    if is_json:
        return value
    try:
        return int(value)
    except TypeError, ValueError:
        raise PluginError("note_invalid", "Supply the note's revision.") from None
