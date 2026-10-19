from datetime import UTC, datetime

from toolframe_sdk import Plugin, PluginError

from .plugin import SERVICE


class Notes(Plugin):
    async def capture(self, body):
        if not isinstance(body, str) or not 1 <= len(body.strip()) <= 1000:
            raise PluginError(
                "note_invalid", "A note needs 1–1000 nonblank characters."
            )
        body = body.strip()
        async with self.context.sql().transaction() as tx:
            created = await tx.execute(
                "INSERT INTO notes(body, created_at) VALUES (?, ?)",
                (body, datetime.now(UTC).isoformat()),
            )
            rows = await tx.query(
                "SELECT id, body, created_at FROM notes WHERE id = ?",
                (created["lastrowid"],),
            )
            return rows[0]

    async def list_notes(self, limit=20):
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise PluginError("note_invalid", "limit must be an integer from 1 to 100.")
        notes = await self.context.sql().query(
            "SELECT id, body, created_at FROM notes ORDER BY id DESC LIMIT ?", (limit,)
        )
        return {"notes": notes, "count": len(notes)}

    async def invoke(self, service, method, params):
        if service == SERVICE:
            if method == "capture":
                return await self.capture(**params)
            if method == "list_notes":
                return await self.list_notes(**params)
        raise PluginError("method_not_found", f"Unknown method {service}/{method}")

    async def health(self):
        await self.context.sql().query("SELECT id FROM notes LIMIT 1")
        return {"runtime": "ready", "business": "ready"}
