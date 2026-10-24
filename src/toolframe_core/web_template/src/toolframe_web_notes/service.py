from datetime import UTC, datetime
from uuid import uuid4

from toolframe_sdk import Plugin, PluginError

from .contracts import SERVICE


def body_text(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 1000:
        raise PluginError("note_invalid", "Enter 1–1000 nonblank characters.")
    return value.strip()


class Notes(Plugin):
    async def invoke(self, service, method, params):
        if service != SERVICE:
            raise PluginError("method_not_found", "Unknown service")
        if method == "list":
            return {
                "notes": await self.context.sql().query(
                    "SELECT id, body, revision, created_at FROM notes ORDER BY created_at DESC LIMIT 100"
                )
            }
        if method == "create":
            body, request_id = body_text(params.get("body")), params.get("request_id")
            if not isinstance(request_id, str) or not 16 <= len(request_id) <= 64:
                raise PluginError("note_invalid", "A stable request ID is required.")
            async with self.context.sql().transaction() as tx:
                rows = await tx.query(
                    "SELECT id, body, revision, created_at FROM notes WHERE request_id = ?",
                    (request_id,),
                )
                if rows:
                    if rows[0]["body"] != body:
                        raise PluginError(
                            "note_conflict",
                            "This request ID already captured different text.",
                        )
                    return rows[0]
                value = {
                    "id": uuid4().hex,
                    "body": body,
                    "revision": 1,
                    "created_at": datetime.now(UTC).isoformat(),
                }
                await tx.execute(
                    "INSERT INTO notes(id, body, revision, created_at, request_id) VALUES (?, ?, ?, ?, ?)",
                    (*value.values(), request_id),
                )
                return value
        if method == "update":
            body = body_text(params.get("body"))
            revision = params.get("revision")
            if (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 1
            ):
                raise PluginError("note_invalid", "Supply the note's revision.")
            async with self.context.sql().transaction() as tx:
                result = await tx.execute(
                    "UPDATE notes SET body = ?, revision = revision + 1 WHERE id = ? AND revision = ?",
                    (body, params.get("id"), revision),
                )
                if result["rowcount"] != 1:
                    raise PluginError(
                        "note_conflict",
                        "This note changed. Review its current text before editing again.",
                    )
                return (
                    await tx.query(
                        "SELECT id, body, revision, created_at FROM notes WHERE id = ?",
                        (params.get("id"),),
                    )
                )[0]
        raise PluginError("method_not_found", "Unknown method")

    async def health(self):
        await self.context.sql().query("SELECT id FROM notes LIMIT 1")
        return {"runtime": "ready", "business": "ready"}
