# Notes

Capture a note of 1–1000 characters from the Notes dashboard. Notes are immutable;
this example does not edit or delete them. The dashboard shows the newest 20.

The read-only MCP tool `sample-notes__list_notes` accepts an optional integer
`limit` from 1 to 100, default 20. It returns `notes` and `count`, where `count` is
the number in this response. Each note has an `id`, `body` and UTC `created_at`.
The MCP tool does not create notes.

Notes remain stored when the plugin is disabled or reloaded. If a capture's result
is uncertain after a lost connection, inspect the latest notes before submitting
again; this sample does not deduplicate separate capture requests.
