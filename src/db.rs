use std::{fs, path::Path};

use anyhow::{Context, Result};
use rusqlite::Connection;

pub fn initialize(path: &Path) -> Result<()> {
    if let Some(parent) = path.parent()
        && !parent.as_os_str().is_empty()
    {
        fs::create_dir_all(parent)
            .with_context(|| format!("could not create database directory {}", parent.display()))?;
    }

    let connection = Connection::open(path)
        .with_context(|| format!("could not open SQLite database {}", path.display()))?;
    connection
        .execute_batch(
            "PRAGMA journal_mode = WAL;
             PRAGMA foreign_keys = ON;
             CREATE TABLE IF NOT EXISTS _lite_mcp_meta (
                 schema_version INTEGER PRIMARY KEY,
                 applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
             );
             INSERT OR IGNORE INTO _lite_mcp_meta(schema_version) VALUES (1);",
        )
        .context("could not initialize SQLite metadata")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn initializes_database_idempotently() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let path = directory.path().join("nested").join("test.db");

        initialize(&path).expect("first initialization succeeds");
        initialize(&path).expect("second initialization succeeds");

        let connection = Connection::open(path).expect("open initialized database");
        let version: i64 = connection
            .query_row("SELECT schema_version FROM _lite_mcp_meta", [], |row| {
                row.get(0)
            })
            .expect("read schema version");
        assert_eq!(version, 1);
    }
}
