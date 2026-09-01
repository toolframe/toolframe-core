use std::{
    fs,
    path::{Path, PathBuf},
    time::Duration,
};

use chrono::{SecondsFormat, Utc};
use rusqlite::{Connection, types::ValueRef};
use serde::Serialize;
use serde_json::{Number, Value};
use thiserror::Error;

const BUSY_TIMEOUT: Duration = Duration::from_secs(5);
const MAX_SQL_BYTES: usize = 1_048_576;
pub const SQL_RESULT_ROW_LIMIT: usize = 100;
pub const TABLE_PREVIEW_ROW_LIMIT: usize = 25;

#[derive(Debug, Error)]
pub enum DatabaseError {
    #[error("SQL must not be empty")]
    EmptySql,
    #[error("SQL exceeds the {MAX_SQL_BYTES} byte limit")]
    SqlTooLong,
    #[error("could not create database directory {path}: {source}")]
    CreateDirectory {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error(transparent)]
    Sqlite(#[from] rusqlite::Error),
}

pub type Result<T> = std::result::Result<T, DatabaseError>;

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct DatabaseSnapshot {
    pub database_path: String,
    pub generated_at: String,
    pub tables: Vec<TablePreview>,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct TablePreview {
    pub name: String,
    pub columns: Vec<String>,
    pub rows: Vec<Vec<Value>>,
    pub truncated: bool,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum SqlExecutionResult {
    Query {
        columns: Vec<String>,
        rows: Vec<Vec<Value>>,
        truncated: bool,
    },
    Mutation {
        affected_rows: usize,
        last_insert_rowid: i64,
    },
}

pub fn initialize(path: &Path) -> Result<()> {
    ensure_parent_directory(path)?;
    let connection = open(path)?;
    connection.execute_batch(
        "PRAGMA journal_mode = WAL;
         CREATE TABLE IF NOT EXISTS _lite_mcp_meta (
             schema_version INTEGER PRIMARY KEY,
             applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
         );
         INSERT OR IGNORE INTO _lite_mcp_meta(schema_version) VALUES (1);",
    )?;
    Ok(())
}

pub fn snapshot(path: &Path) -> Result<DatabaseSnapshot> {
    let connection = open(path)?;
    let table_names = user_table_names(&connection)?;
    let mut tables = Vec::with_capacity(table_names.len());

    for name in table_names {
        tables.push(preview_table(&connection, name)?);
    }

    Ok(DatabaseSnapshot {
        database_path: display_path(path),
        generated_at: Utc::now().to_rfc3339_opts(SecondsFormat::Secs, true),
        tables,
    })
}

pub fn execute_sql(path: &Path, sql: &str) -> Result<SqlExecutionResult> {
    let sql = sql.trim();
    if sql.is_empty() {
        return Err(DatabaseError::EmptySql);
    }
    if sql.len() > MAX_SQL_BYTES {
        return Err(DatabaseError::SqlTooLong);
    }

    let connection = open(path)?;
    let mut statement = connection.prepare(sql)?;

    if statement.column_count() == 0 {
        let affected_rows = statement.execute([])?;
        return Ok(SqlExecutionResult::Mutation {
            affected_rows,
            last_insert_rowid: connection.last_insert_rowid(),
        });
    }

    let columns = statement
        .column_names()
        .into_iter()
        .map(str::to_owned)
        .collect::<Vec<_>>();
    let column_count = columns.len();
    let mut cursor = statement.query([])?;
    let mut rows = Vec::new();

    while rows.len() <= SQL_RESULT_ROW_LIMIT {
        let Some(row) = cursor.next()? else {
            break;
        };
        rows.push(read_row(row, column_count)?);
    }

    let truncated = rows.len() > SQL_RESULT_ROW_LIMIT;
    rows.truncate(SQL_RESULT_ROW_LIMIT);

    Ok(SqlExecutionResult::Query {
        columns,
        rows,
        truncated,
    })
}

fn ensure_parent_directory(path: &Path) -> Result<()> {
    let Some(parent) = path.parent() else {
        return Ok(());
    };
    if parent.as_os_str().is_empty() {
        return Ok(());
    }
    fs::create_dir_all(parent).map_err(|source| DatabaseError::CreateDirectory {
        path: parent.to_path_buf(),
        source,
    })
}

fn open(path: &Path) -> Result<Connection> {
    let connection = Connection::open(path)?;
    connection.busy_timeout(BUSY_TIMEOUT)?;
    connection.execute_batch("PRAGMA foreign_keys = ON;")?;
    Ok(connection)
}

fn user_table_names(connection: &Connection) -> Result<Vec<String>> {
    let mut statement = connection.prepare(
        "SELECT name
         FROM sqlite_schema
         WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
         ORDER BY name COLLATE NOCASE",
    )?;
    let names = statement
        .query_map([], |row| row.get::<_, String>(0))?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    Ok(names)
}

fn preview_table(connection: &Connection, name: String) -> Result<TablePreview> {
    let quoted_name = quote_identifier(&name);
    let sql = format!(
        "SELECT * FROM {quoted_name} LIMIT {}",
        TABLE_PREVIEW_ROW_LIMIT + 1
    );
    let mut statement = connection.prepare(&sql)?;
    let columns = statement
        .column_names()
        .into_iter()
        .map(str::to_owned)
        .collect::<Vec<_>>();
    let column_count = columns.len();
    let mut cursor = statement.query([])?;
    let mut rows = Vec::new();

    while let Some(row) = cursor.next()? {
        rows.push(read_row(row, column_count)?);
    }

    let truncated = rows.len() > TABLE_PREVIEW_ROW_LIMIT;
    rows.truncate(TABLE_PREVIEW_ROW_LIMIT);

    Ok(TablePreview {
        name,
        columns,
        rows,
        truncated,
    })
}

fn read_row(row: &rusqlite::Row<'_>, column_count: usize) -> Result<Vec<Value>> {
    (0..column_count)
        .map(|index| row.get_ref(index).map(sqlite_value).map_err(Into::into))
        .collect()
}

fn sqlite_value(value: ValueRef<'_>) -> Value {
    match value {
        ValueRef::Null => Value::Null,
        ValueRef::Integer(value) => Value::Number(value.into()),
        ValueRef::Real(value) => Number::from_f64(value)
            .map(Value::Number)
            .unwrap_or(Value::Null),
        ValueRef::Text(value) => Value::String(String::from_utf8_lossy(value).into_owned()),
        ValueRef::Blob(value) => Value::String(format!("<blob: {} bytes>", value.len())),
    }
}

fn quote_identifier(identifier: &str) -> String {
    format!("\"{}\"", identifier.replace('"', "\"\""))
}

fn display_path(path: &Path) -> String {
    path.canonicalize()
        .unwrap_or_else(|_| path.to_path_buf())
        .to_string_lossy()
        .into_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_database() -> (tempfile::TempDir, PathBuf) {
        let directory = tempfile::tempdir().expect("create temp directory");
        let path = directory.path().join("nested").join("test.db");
        (directory, path)
    }

    #[test]
    fn initializes_database_idempotently() {
        let (_directory, path) = temp_database();

        initialize(&path).expect("first migration succeeds");
        initialize(&path).expect("second migration succeeds");

        let snapshot = snapshot(&path).expect("snapshot succeeds");
        assert_eq!(snapshot.tables.len(), 1);
        assert_eq!(snapshot.tables[0].name, "_lite_mcp_meta");
        assert_eq!(snapshot.tables[0].rows.len(), 1);
    }

    #[test]
    fn executes_mutations_and_queries() {
        let (_directory, path) = temp_database();
        initialize(&path).expect("migration succeeds");

        let created = execute_sql(
            &path,
            "CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT, note TEXT)",
        )
        .expect("create table succeeds");
        assert!(matches!(created, SqlExecutionResult::Mutation { .. }));

        execute_sql(&path, "INSERT INTO people(name, note) VALUES ('Ada', NULL)")
            .expect("insert succeeds");

        let queried = execute_sql(&path, "SELECT name, note FROM people").expect("select succeeds");
        assert_eq!(
            queried,
            SqlExecutionResult::Query {
                columns: vec!["name".into(), "note".into()],
                rows: vec![vec![Value::String("Ada".into()), Value::Null]],
                truncated: false,
            }
        );
    }

    #[test]
    fn previews_quoted_table_names_and_caps_rows() {
        let (_directory, path) = temp_database();
        initialize(&path).expect("migration succeeds");
        execute_sql(&path, "CREATE TABLE \"odd\"\"name\" (value INTEGER)")
            .expect("create quoted table succeeds");

        let connection = open(&path).expect("open succeeds");
        let transaction = connection
            .unchecked_transaction()
            .expect("start transaction");
        for value in 0..=TABLE_PREVIEW_ROW_LIMIT {
            transaction
                .execute(
                    "INSERT INTO \"odd\"\"name\"(value) VALUES (?)",
                    [value as i64],
                )
                .expect("insert succeeds");
        }
        transaction.commit().expect("commit succeeds");

        let snapshot = snapshot(&path).expect("snapshot succeeds");
        let table = snapshot
            .tables
            .iter()
            .find(|table| table.name == "odd\"name")
            .expect("quoted table appears");
        assert_eq!(table.rows.len(), TABLE_PREVIEW_ROW_LIMIT);
        assert!(table.truncated);
    }

    #[test]
    fn rejects_empty_or_invalid_sql_without_damaging_database() {
        let (_directory, path) = temp_database();
        initialize(&path).expect("migration succeeds");

        assert!(matches!(
            execute_sql(&path, "   "),
            Err(DatabaseError::EmptySql)
        ));
        assert!(execute_sql(&path, "SELECT FROM definitely_invalid").is_err());
        assert!(snapshot(&path).is_ok());
    }

    #[test]
    fn caps_query_results() {
        let (_directory, path) = temp_database();
        initialize(&path).expect("migration succeeds");
        execute_sql(&path, "CREATE TABLE numbers (value INTEGER)").expect("create table succeeds");
        let connection = open(&path).expect("open succeeds");
        let transaction = connection
            .unchecked_transaction()
            .expect("start transaction");
        for value in 0..=SQL_RESULT_ROW_LIMIT {
            transaction
                .execute("INSERT INTO numbers(value) VALUES (?)", [value as i64])
                .expect("insert succeeds");
        }
        transaction.commit().expect("commit succeeds");

        let result =
            execute_sql(&path, "SELECT value FROM numbers ORDER BY value").expect("query succeeds");
        match result {
            SqlExecutionResult::Query {
                rows, truncated, ..
            } => {
                assert_eq!(rows.len(), SQL_RESULT_ROW_LIMIT);
                assert!(truncated);
            }
            SqlExecutionResult::Mutation { .. } => panic!("expected query result"),
        }
    }
}
