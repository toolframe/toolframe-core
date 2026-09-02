-- Durable successful results for explicit agent-authored business mutations.
-- The helper commits this receipt in the same transaction as the mutation.

CREATE TABLE _lite_mcp_idempotency (
    namespace TEXT NOT NULL,
    business_key TEXT NOT NULL,
    input_sha256 TEXT NOT NULL CHECK (length(input_sha256) = 64),
    result_json BLOB NOT NULL,
    result_sha256 TEXT NOT NULL CHECK (length(result_sha256) = 64),
    result_bytes INTEGER NOT NULL CHECK (result_bytes BETWEEN 1 AND 1048576),
    original_operation_id TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ')),
    PRIMARY KEY (namespace, business_key)
) STRICT, WITHOUT ROWID;
