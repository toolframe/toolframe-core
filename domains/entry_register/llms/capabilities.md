# Entry Register capabilities

## Peer-agent query

`entry_register_list_entries(limit=50)` returns up to 100 newest entries with
their IDs, labels, numeric values, versions, and timestamps. It is read-only.

There is no generic SQL, schema, repair, create, update, or delete capability.
If another business action is required, report the missing intent to the owner
so the master development agent can add a narrow domain tool.

## Human dashboard

The Entry Register card lists, creates, and version-guards edits. Expected input
or concurrency errors appear in one copyable error card. Programming defects
stop the app and must be repaired by the master development agent.
