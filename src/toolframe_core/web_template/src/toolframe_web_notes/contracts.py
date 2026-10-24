"""Public data contracts shared by service declarations and presentation adapters."""

SERVICE = "web-notes.notes"
BODY = {"type": "string", "minLength": 1, "maxLength": 1000, "pattern": r"\S"}
NOTE_ID = {"type": "string", "pattern": "^[a-f0-9]{32}$"}
REVISION = {"type": "integer", "minimum": 1}
REQUEST_ID = {"type": "string", "minLength": 16, "maxLength": 64}

NOTE = {
    "type": "object",
    "properties": {
        "id": NOTE_ID,
        "body": BODY,
        "revision": REVISION,
        "created_at": {"type": "string", "minLength": 20, "maxLength": 40},
    },
    "required": ["id", "body", "revision", "created_at"],
    "additionalProperties": False,
}

INPUTS = {
    "list": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    "create": {
        "type": "object",
        "properties": {"body": BODY, "request_id": REQUEST_ID},
        "required": ["body", "request_id"],
        "additionalProperties": False,
    },
    "update": {
        "type": "object",
        "properties": {"id": NOTE_ID, "body": BODY, "revision": REVISION},
        "required": ["id", "body", "revision"],
        "additionalProperties": False,
    },
}
OUTPUTS = {
    "list": {
        "type": "object",
        "properties": {"notes": {"type": "array", "items": NOTE, "maxItems": 100}},
        "required": ["notes"],
        "additionalProperties": False,
    },
    "create": NOTE,
    "update": NOTE,
}
