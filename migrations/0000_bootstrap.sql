-- Baseline schema for the editable Toolframe dashboard example.
--
-- The Rust launcher owns the transaction and migration ledger. Do not add
-- BEGIN, COMMIT, ROLLBACK, SAVEPOINT, PRAGMA, ATTACH, DETACH, or CREATE
-- TRIGGER statements.
-- Never edit this file after it has been applied; add 0001_<description>.sql.

CREATE TABLE IF NOT EXISTS dashboard_example_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL CHECK (length(trim(label)) BETWEEN 1 AND 80),
    value REAL NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
