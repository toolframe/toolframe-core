"""Keep callable service contracts useful to every presentation and agent."""

import pytest
from jsonschema import ValidationError
from toolframe_sdk import validate
from toolframe_web_notes.plugin import plugin


@pytest.mark.parametrize(
    "method,payload",
    [
        ("list", {}),
        ("create", {"body": "Remember this", "request_id": "a" * 32}),
        ("update", {"id": "b" * 32, "body": "Edited", "revision": 1}),
    ],
)
def test_inputs_are_described_and_bounded(method, payload):
    spec = plugin.services["web-notes.notes"].methods[method]
    assert spec.description and not spec.public_api
    validate(spec.input_schema, payload)
    with pytest.raises(ValidationError):
        validate(spec.input_schema, {**payload, "unexpected": True})


def test_notes_require_real_revisions_and_closed_outputs():
    spec = plugin.services["web-notes.notes"].methods["update"]
    with pytest.raises(ValidationError):
        validate(spec.input_schema, {"id": "a" * 32, "body": "Text", "revision": True})
    with pytest.raises(ValidationError):
        validate(spec.output_schema, {"id": "a" * 32, "body": "Text"})
