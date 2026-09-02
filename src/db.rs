use std::{
    fs,
    io::Read,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

use anyhow::{Context, Result, anyhow, bail};
use rusqlite::{
    Connection, Error as SqliteError, ErrorCode, Transaction, TransactionBehavior,
    hooks::{AuthAction, AuthContext, Authorization},
    params,
};
use sha2::{Digest, Sha256};

const BOOTSTRAP_FILENAME: &str = "0000_bootstrap.sql";
const LEDGER_TABLE: &str = "_lite_mcp_migrations";
const MAX_MIGRATION_BYTES: u64 = 4 * 1024 * 1024;
const BUSY_TIMEOUT: Duration = Duration::from_secs(5);
const LEDGER_SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS _lite_mcp_migrations (
    version INTEGER PRIMARY KEY CHECK (version BETWEEN 0 AND 9999),
    filename TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ'))
) STRICT;
"#;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MigrationReport {
    pub applied_now: usize,
    pub total: usize,
    pub current_version: u16,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MigrationValidation {
    pub total: usize,
    pub current_version: u16,
    pub pending: usize,
    pub database_exists: bool,
}

#[derive(Debug, Clone)]
struct Migration {
    version: u16,
    filename: String,
    path: PathBuf,
    sql: String,
    sha256: String,
}

#[derive(Debug)]
struct AppliedMigration {
    version: u16,
    filename: String,
    sha256: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct DatabaseFileFingerprint {
    path: PathBuf,
    bytes: u64,
    sha256: String,
}

struct DatabaseSnapshot {
    _directory: tempfile::TempDir,
    database: PathBuf,
}

/// Validate and atomically apply every pending migration before app startup.
pub fn migrate(database_path: &Path, migrations_dir: &Path) -> Result<MigrationReport> {
    let migrations = discover_migrations(migrations_dir)?;
    if let Some(parent) = database_path.parent()
        && !parent.as_os_str().is_empty()
    {
        fs::create_dir_all(parent).with_context(|| {
            format!(
                "could not create database directory {} (phase=migration, retryable=false)",
                parent.display()
            )
        })?;
    }

    let mut connection = Connection::open(database_path).map_err(|error| {
        sqlite_failure(
            "open",
            None,
            database_path,
            &error,
            "Inspect the database path and permissions.",
        )
    })?;
    connection.busy_timeout(BUSY_TIMEOUT).map_err(|error| {
        sqlite_failure(
            "configure",
            None,
            database_path,
            &error,
            "Inspect the SQLite runtime configuration.",
        )
    })?;
    configure_connection(&connection, database_path)?;

    migrate_connection(&mut connection, database_path, &migrations, true)
}

fn migrate_connection(
    connection: &mut Connection,
    database_path: &Path,
    migrations: &[Migration],
    commit: bool,
) -> Result<MigrationReport> {
    let transaction = connection
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|error| {
            sqlite_failure(
                "begin",
                None,
                database_path,
                &error,
                "Wait for another database writer to finish, then retry if retryable=true.",
            )
        })?;
    transaction.execute_batch(LEDGER_SCHEMA).map_err(|error| {
        sqlite_failure(
            "ledger",
            None,
            database_path,
            &error,
            "Restore the internal migration ledger schema; do not drop or edit it manually.",
        )
    })?;
    validate_ledger_schema(&transaction, database_path)?;

    let applied = load_applied(&transaction, database_path)?;
    validate_applied_history(migrations, &applied)?;
    verify_sources_unchanged(migrations)?;

    let mut applied_now = 0;
    for migration in migrations.iter().skip(applied.len()) {
        apply_one(&transaction, migration)?;
        applied_now += 1;
    }

    if commit {
        transaction.commit().map_err(|error| {
            sqlite_failure(
                "commit",
                migrations.last(),
                database_path,
                &error,
                "Restart lite-mcp; the ledger will determine whether the transaction committed.",
            )
        })?;
    } else {
        transaction
            .rollback()
            .context("could not discard the migration validation snapshot")?;
    }

    Ok(MigrationReport {
        applied_now,
        total: migrations.len(),
        current_version: migrations
            .last()
            .expect("migration discovery requires bootstrap")
            .version,
    })
}

/// Exercise production migration discovery, history validation, and pending
/// application against an in-memory snapshot without changing data.db.
pub fn validate_migrations(
    database_path: &Path,
    migrations_dir: &Path,
) -> Result<MigrationValidation> {
    let migrations = discover_migrations(migrations_dir)?;
    let database_exists = database_path.exists();
    let snapshot = database_exists
        .then(|| copy_database_snapshot(database_path))
        .transpose()?;
    let mut connection = match &snapshot {
        Some(snapshot) => Connection::open(&snapshot.database)
            .context("could not open the copied database validation snapshot")?,
        None => Connection::open_in_memory()
            .context("could not open the in-memory migration validation database")?,
    };
    connection
        .busy_timeout(BUSY_TIMEOUT)
        .context("could not configure the in-memory migration validation database busy timeout")?;
    configure_connection(&connection, database_path)?;
    let report = migrate_connection(&mut connection, database_path, &migrations, false)?;

    Ok(MigrationValidation {
        total: report.total,
        current_version: report.current_version,
        pending: report.applied_now,
        database_exists,
    })
}

fn copy_database_snapshot(database_path: &Path) -> Result<DatabaseSnapshot> {
    let stability_context = || {
        format!(
            "could not obtain stable database files for validation (phase=migration-validation-snapshot, path={}, retryable=true, recoveryHint=inspect file permissions, stop lite-mcp and other database writers, then retry)",
            database_path.display()
        )
    };
    let before = fingerprint_database_files(database_path).with_context(stability_context)?;
    let directory = tempfile::Builder::new()
        .prefix("lite-mcp-database-check-")
        .tempdir()
        .context("could not create a temporary database validation directory")?;
    let filename = database_path.file_name().ok_or_else(|| {
        anyhow!(
            "database path {} has no filename for validation",
            database_path.display()
        )
    })?;
    let database = directory.path().join(filename);

    for source in &before {
        let destination = directory
            .path()
            .join(source.path.file_name().ok_or_else(|| {
                anyhow!(
                    "database file {} has no filename for validation",
                    source.path.display()
                )
            })?);
        copy_regular_file(&source.path, &destination).with_context(stability_context)?;
        let copied = fingerprint_file(&destination).with_context(stability_context)?;
        if copied.bytes != source.bytes || copied.sha256 != source.sha256 {
            bail!(
                "database files changed while check copied {} (phase=migration-validation-snapshot, path={}, retryable=true, recoveryHint=stop lite-mcp and other database writers, then retry)",
                source.path.display(),
                database_path.display()
            );
        }
    }

    let after = fingerprint_database_files(database_path).with_context(stability_context)?;
    if before != after {
        bail!(
            "database files changed while check prepared a validation snapshot (phase=migration-validation-snapshot, path={}, retryable=true, recoveryHint=stop lite-mcp and other database writers, then retry)",
            database_path.display()
        );
    }
    Ok(DatabaseSnapshot {
        _directory: directory,
        database,
    })
}

fn fingerprint_database_files(database_path: &Path) -> Result<Vec<DatabaseFileFingerprint>> {
    let candidates = [
        database_path.to_path_buf(),
        database_sidecar_path(database_path, "-wal"),
        database_sidecar_path(database_path, "-shm"),
    ];
    let mut files = Vec::new();
    for (index, path) in candidates.into_iter().enumerate() {
        match fs::symlink_metadata(&path) {
            Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_file() => {
                bail!(
                    "database snapshot source {} must be a regular file, not a symlink (phase=migration-validation-snapshot, retryable=false)",
                    path.display()
                );
            }
            Ok(_) => files.push(fingerprint_file(&path)?),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound && index > 0 => {}
            Err(error) => {
                return Err(error).with_context(|| {
                    format!(
                        "could not inspect database snapshot source {}",
                        path.display()
                    )
                });
            }
        }
    }
    Ok(files)
}

fn fingerprint_file(path: &Path) -> Result<DatabaseFileFingerprint> {
    let mut file = fs::File::open(path)
        .with_context(|| format!("could not read database snapshot source {}", path.display()))?;
    let mut digest = Sha256::new();
    let mut bytes = 0_u64;
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let read = file
            .read(&mut buffer)
            .with_context(|| format!("could not hash database file {}", path.display()))?;
        if read == 0 {
            break;
        }
        digest.update(&buffer[..read]);
        bytes = bytes.saturating_add(read as u64);
    }
    Ok(DatabaseFileFingerprint {
        path: path.to_path_buf(),
        bytes,
        sha256: format!("{:x}", digest.finalize()),
    })
}

fn copy_regular_file(source: &Path, destination: &Path) -> Result<()> {
    let mut input = fs::File::open(source).with_context(|| {
        format!(
            "could not open database snapshot source {}",
            source.display()
        )
    })?;
    let mut output = fs::OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(destination)
        .with_context(|| {
            format!(
                "could not create database snapshot file {}",
                destination.display()
            )
        })?;
    std::io::copy(&mut input, &mut output).with_context(|| {
        format!(
            "could not copy database snapshot source {}",
            source.display()
        )
    })?;
    output
        .sync_all()
        .with_context(|| format!("could not sync database snapshot {}", destination.display()))?;
    Ok(())
}

fn database_sidecar_path(database_path: &Path, suffix: &str) -> PathBuf {
    let mut path = database_path.as_os_str().to_os_string();
    path.push(suffix);
    PathBuf::from(path)
}

pub fn next_migration_filename(migrations_dir: &Path, description: &str) -> Result<String> {
    let migrations = discover_migrations(migrations_dir)?;
    let version = u16::try_from(migrations.len())
        .context("migration sequence is too long for four-digit versions")?;
    if version > 9999 {
        bail!("migration sequence has exhausted four-digit versions");
    }
    let filename = format!("{version:04}_{description}.sql");
    parse_filename(&filename)
        .with_context(|| format!("invalid migration description {description:?}"))?;
    Ok(filename)
}

fn configure_connection(connection: &Connection, database_path: &Path) -> Result<()> {
    connection
        .execute_batch("PRAGMA foreign_keys = ON;")
        .map_err(|error| {
            sqlite_failure(
                "configure",
                None,
                database_path,
                &error,
                "Ensure data.db is writable and not held by an incompatible process.",
            )
        })?;

    let started = Instant::now();
    loop {
        match connection.execute_batch("PRAGMA journal_mode = WAL;") {
            Ok(()) => return Ok(()),
            Err(error) if is_busy_or_locked(&error) && started.elapsed() < BUSY_TIMEOUT => {
                let remaining = BUSY_TIMEOUT.saturating_sub(started.elapsed());
                std::thread::sleep(remaining.min(Duration::from_millis(10)));
            }
            Err(error) => {
                return Err(sqlite_failure(
                    "configure",
                    None,
                    database_path,
                    &error,
                    "Ensure data.db is writable and not held by an incompatible process.",
                ));
            }
        }
    }
}

fn discover_migrations(directory: &Path) -> Result<Vec<Migration>> {
    let metadata = fs::symlink_metadata(directory).with_context(|| {
        format!(
            "could not inspect migration directory {} (phase=migration-discovery, retryable=true)",
            directory.display()
        )
    })?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        bail!(
            "migration directory {} must be a regular directory, not a symlink (phase=migration-discovery, retryable=false)",
            directory.display()
        );
    }

    let mut migrations = Vec::new();
    for entry in fs::read_dir(directory).with_context(|| {
        format!(
            "could not read migration directory {} (phase=migration-discovery, retryable=true)",
            directory.display()
        )
    })? {
        let entry = entry.with_context(|| {
            format!(
                "could not inspect an entry in {} (phase=migration-discovery, retryable=true)",
                directory.display()
            )
        })?;
        let filename = entry.file_name().into_string().map_err(|_| {
            anyhow!(
                "migration directory {} contains a non-UTF-8 filename (phase=migration-discovery, retryable=false)",
                directory.display()
            )
        })?;
        if filename.starts_with(['.', '_']) || !filename.ends_with(".sql") {
            continue;
        }

        let file_type = entry.file_type().with_context(|| {
            format!(
                "could not inspect migration {} (phase=migration-discovery, retryable=true)",
                entry.path().display()
            )
        })?;
        if file_type.is_symlink() || !file_type.is_file() {
            bail!(
                "migration {} must be a direct regular file, not a symlink or directory (phase=migration-discovery, retryable=false)",
                entry.path().display()
            );
        }

        let version = parse_filename(&filename).with_context(|| {
            format!(
                "invalid migration filename {} (phase=migration-discovery, retryable=false)",
                entry.path().display()
            )
        })?;
        let file_metadata = entry.metadata().with_context(|| {
            format!(
                "could not read migration metadata {} (phase=migration-discovery, retryable=true)",
                entry.path().display()
            )
        })?;
        if file_metadata.len() > MAX_MIGRATION_BYTES {
            bail!(
                "migration {} exceeds the {} byte limit (phase=migration-discovery, retryable=false)",
                entry.path().display(),
                MAX_MIGRATION_BYTES
            );
        }
        let bytes = read_migration_bytes(&entry.path(), version, "discovery", false)?;
        let sha256 = sha256(&bytes);
        let sql = String::from_utf8(bytes).map_err(|error| {
            anyhow!(
                "migration {} must be UTF-8 (phase=migration-discovery, retryable=false): {error}",
                entry.path().display()
            )
        })?;
        migrations.push(Migration {
            version,
            filename,
            path: entry.path(),
            sql,
            sha256,
        });
    }

    migrations.sort_by(|left, right| {
        left.version
            .cmp(&right.version)
            .then_with(|| left.filename.cmp(&right.filename))
    });
    for pair in migrations.windows(2) {
        if pair[0].version == pair[1].version {
            bail!(
                "duplicate migration version {:04}: {} and {} (phase=migration-discovery, retryable=false)",
                pair[0].version,
                pair[0].path.display(),
                pair[1].path.display()
            );
        }
    }
    let Some(first) = migrations.first() else {
        bail!(
            "migration directory {} must contain {} (phase=migration-discovery, retryable=false)",
            directory.display(),
            BOOTSTRAP_FILENAME
        );
    };
    if first.filename != BOOTSTRAP_FILENAME {
        bail!(
            "migration version 0000 must be named {} (found {}; phase=migration-discovery, retryable=false)",
            BOOTSTRAP_FILENAME,
            first.path.display()
        );
    }
    for (expected, migration) in migrations.iter().enumerate() {
        let expected = u16::try_from(expected).expect("four-digit migration count fits u16");
        if migration.version != expected {
            bail!(
                "migration sequence is not contiguous: expected {expected:04}, found {:04} at {} (phase=migration-discovery, retryable=false)",
                migration.version,
                migration.path.display()
            );
        }
    }
    Ok(migrations)
}

fn parse_filename(filename: &str) -> Result<u16> {
    let stem = filename
        .strip_suffix(".sql")
        .ok_or_else(|| anyhow!("filename must end in .sql"))?;
    let bytes = stem.as_bytes();
    if bytes.len() < 6 || !bytes[..4].iter().all(u8::is_ascii_digit) || bytes[4] != b'_' {
        bail!("expected NNNN_description.sql");
    }
    let description = &bytes[5..];
    if !description[0].is_ascii_lowercase() && !description[0].is_ascii_digit() {
        bail!("description must begin with a lowercase ASCII letter or digit");
    }
    if !description.iter().all(|byte| {
        byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'_' | b'-')
    }) {
        bail!("description may contain only lowercase ASCII letters, digits, '_' or '-'");
    }
    stem[..4].parse().context("invalid four-digit version")
}

fn validate_ledger_schema(transaction: &Transaction<'_>, database_path: &Path) -> Result<()> {
    let actual = transaction
        .query_row(
            "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?1",
            [LEDGER_TABLE],
            |row| row.get::<_, String>(0),
        )
        .map_err(|error| {
            sqlite_failure(
                "ledger",
                None,
                database_path,
                &error,
                "Restore the internal migration ledger schema; do not edit it manually.",
            )
        })?;
    if schema_signature(&actual) != schema_signature(LEDGER_SCHEMA) {
        bail!(
            "internal migration ledger has an incompatible schema (phase=migration-ledger, path={}, retryable=false, recoveryHint=restore the original _lite_mcp_migrations table; do not drop or edit migration history)",
            database_path.display()
        );
    }
    Ok(())
}

fn schema_signature(sql: &str) -> String {
    sql.split_ascii_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .replacen("CREATE TABLE IF NOT EXISTS", "CREATE TABLE", 1)
        .trim_end_matches(';')
        .to_owned()
}

fn load_applied(
    transaction: &Transaction<'_>,
    database_path: &Path,
) -> Result<Vec<AppliedMigration>> {
    let mut statement = transaction
        .prepare(&format!(
            "SELECT version, filename, sha256 FROM {LEDGER_TABLE} ORDER BY version"
        ))
        .map_err(|error| {
            sqlite_failure(
                "ledger",
                None,
                database_path,
                &error,
                "Restore the internal migration ledger schema; do not edit it manually.",
            )
        })?;
    let rows = statement
        .query_map([], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
            ))
        })
        .map_err(|error| {
            sqlite_failure(
                "ledger",
                None,
                database_path,
                &error,
                "Restore the internal migration ledger schema; do not edit it manually.",
            )
        })?;

    let mut applied = Vec::new();
    for row in rows {
        let (version, filename, sha256) = row.map_err(|error| {
            sqlite_failure(
                "ledger",
                None,
                database_path,
                &error,
                "Restore the internal migration ledger schema; do not edit it manually.",
            )
        })?;
        let version = u16::try_from(version).map_err(|_| {
            anyhow!(
                "migration ledger contains invalid version {version} (phase=migration-ledger, retryable=false)"
            )
        })?;
        applied.push(AppliedMigration {
            version,
            filename,
            sha256,
        });
    }
    Ok(applied)
}

fn validate_applied_history(migrations: &[Migration], applied: &[AppliedMigration]) -> Result<()> {
    for (expected, record) in applied.iter().enumerate() {
        if usize::from(record.version) != expected {
            bail!(
                "migration ledger has a history gap: expected {expected:04}, found {:04} (phase=migration-ledger, retryable=false)",
                record.version
            );
        }
        let Some(source) = migrations.get(expected) else {
            bail!(
                "applied migration {:04} ({}) is missing from the migrations directory (phase=migration-integrity, retryable=false, recoveryHint=restore the original file)",
                record.version,
                record.filename
            );
        };
        if source.filename != record.filename {
            bail!(
                "applied migration {:04} was renamed: ledger has {}, source has {} (phase=migration-integrity, retryable=false, recoveryHint=restore the original filename)",
                record.version,
                record.filename,
                source.filename
            );
        }
        if source.sha256 != record.sha256 {
            bail!(
                "applied migration {:04} checksum changed at {} (expected {}, found {}; phase=migration-integrity, retryable=false, recoveryHint=restore the applied file and add a new migration)",
                record.version,
                source.path.display(),
                record.sha256,
                source.sha256
            );
        }
    }
    Ok(())
}

fn verify_sources_unchanged(migrations: &[Migration]) -> Result<()> {
    for migration in migrations {
        let bytes = read_migration_bytes(&migration.path, migration.version, "integrity", true)?;
        let current = sha256(&bytes);
        if current != migration.sha256 {
            bail!(
                "migration {} changed during startup (phase=migration-integrity, version={:04}, retryable=true)",
                migration.path.display(),
                migration.version
            );
        }
    }
    Ok(())
}

fn read_migration_bytes(
    path: &Path,
    version: u16,
    phase: &str,
    changed_during_startup: bool,
) -> Result<Vec<u8>> {
    let file = fs::File::open(path).with_context(|| {
        format!(
            "could not open migration {} (phase=migration-{phase}, version={version:04}, retryable=true)",
            path.display()
        )
    })?;
    let mut bytes = Vec::new();
    file.take(MAX_MIGRATION_BYTES + 1)
        .read_to_end(&mut bytes)
        .with_context(|| {
            format!(
                "could not read migration {} (phase=migration-{phase}, version={version:04}, retryable=true)",
                path.display()
            )
        })?;
    if bytes.len() as u64 > MAX_MIGRATION_BYTES {
        bail!(
            "migration {} exceeds the {} byte limit{} (phase=migration-{phase}, version={version:04}, retryable={changed_during_startup})",
            path.display(),
            MAX_MIGRATION_BYTES,
            if changed_during_startup {
                " after discovery"
            } else {
                ""
            }
        );
    }
    Ok(bytes)
}

fn apply_one(transaction: &Transaction<'_>, migration: &Migration) -> Result<()> {
    let denial = Arc::new(Mutex::new(None));
    let captured_denial = Arc::clone(&denial);
    transaction
        .authorizer(Some(move |context: AuthContext<'_>| {
            authorize_migration(context, &captured_denial)
        }))
        .map_err(|error| {
            sqlite_failure(
                "authorizer",
                Some(migration),
                &migration.path,
                &error,
                "Inspect the bundled SQLite hooks support.",
            )
        })?;

    let execution = transaction.execute_batch(&migration.sql);
    transaction
        .authorizer(None::<fn(AuthContext<'_>) -> Authorization>)
        .map_err(|error| {
            sqlite_failure(
                "authorizer-reset",
                Some(migration),
                &migration.path,
                &error,
                "Restart lite-mcp before attempting another migration.",
            )
        })?;

    if let Err(error) = execution {
        if let Some(reason) = *denial
            .lock()
            .expect("migration denial lock is not poisoned")
        {
            bail!(
                "migration {} was rejected (phase=migration-sql, version={:04}, checksum={}, retryable=false): {reason}",
                migration.path.display(),
                migration.version,
                migration.sha256
            );
        }
        return Err(sqlite_failure(
            "sql",
            Some(migration),
            &migration.path,
            &error,
            "Correct the migration SQL, or restore the original applied file and add a new migration.",
        ));
    }

    transaction
        .execute(
            &format!("INSERT INTO {LEDGER_TABLE}(version, filename, sha256) VALUES (?1, ?2, ?3)"),
            params![
                i64::from(migration.version),
                migration.filename,
                migration.sha256
            ],
        )
        .map_err(|error| {
            sqlite_failure(
                "ledger-record",
                Some(migration),
                &migration.path,
                &error,
                "Restore the internal migration ledger schema; do not edit it manually.",
            )
        })?;
    Ok(())
}

fn authorize_migration(
    context: AuthContext<'_>,
    denial: &Arc<Mutex<Option<&'static str>>>,
) -> Authorization {
    let reason = match context.action {
        AuthAction::Transaction { .. } | AuthAction::Savepoint { .. } => {
            Some("transaction statements are not allowed; the launcher owns one atomic transaction")
        }
        AuthAction::Pragma { .. } => {
            Some("PRAGMA statements are not allowed; the launcher owns SQLite configuration")
        }
        AuthAction::Attach { .. } | AuthAction::Detach { .. } => {
            Some("ATTACH and DETACH are not allowed in migrations")
        }
        AuthAction::CreateTrigger { .. } | AuthAction::CreateTempTrigger { .. } => Some(
            "CREATE TRIGGER is not allowed because deferred trigger bodies can bypass migration ledger protection",
        ),
        action if action_targets_ledger(action) => {
            Some("migration SQL must not read or modify the internal migration ledger")
        }
        _ => None,
    };
    if let Some(reason) = reason {
        let mut captured = denial
            .lock()
            .expect("migration denial lock is not poisoned");
        if captured.is_none() {
            *captured = Some(reason);
        }
        Authorization::Deny
    } else {
        Authorization::Allow
    }
}

fn action_targets_ledger(action: AuthAction<'_>) -> bool {
    let table_name = match action {
        AuthAction::CreateIndex { table_name, .. }
        | AuthAction::CreateTable { table_name }
        | AuthAction::CreateTempIndex { table_name, .. }
        | AuthAction::CreateTempTable { table_name }
        | AuthAction::CreateTempTrigger { table_name, .. }
        | AuthAction::CreateTrigger { table_name, .. }
        | AuthAction::Delete { table_name }
        | AuthAction::DropIndex { table_name, .. }
        | AuthAction::DropTable { table_name }
        | AuthAction::DropTempIndex { table_name, .. }
        | AuthAction::DropTempTable { table_name }
        | AuthAction::DropTempTrigger { table_name, .. }
        | AuthAction::DropTrigger { table_name, .. }
        | AuthAction::Insert { table_name }
        | AuthAction::Read { table_name, .. }
        | AuthAction::Update { table_name, .. }
        | AuthAction::CreateVtable { table_name, .. }
        | AuthAction::DropVtable { table_name, .. }
        | AuthAction::Analyze { table_name } => Some(table_name),
        AuthAction::AlterTable { table_name, .. } => Some(table_name),
        _ => None,
    };
    table_name.is_some_and(|name| name.eq_ignore_ascii_case(LEDGER_TABLE))
}

fn sqlite_failure(
    phase: &str,
    migration: Option<&Migration>,
    path: &Path,
    error: &rusqlite::Error,
    recovery_hint: &str,
) -> anyhow::Error {
    let error_code = sqlite_error_code(error);
    let retryable = matches!(
        error_code,
        Some(
            ErrorCode::DatabaseBusy
                | ErrorCode::DatabaseLocked
                | ErrorCode::SystemIoFailure
                | ErrorCode::DiskFull
        )
    );
    let version = migration
        .map(|migration| format!(", version={:04}", migration.version))
        .unwrap_or_default();
    let checksum = migration
        .map(|migration| format!(", checksum={}", migration.sha256))
        .unwrap_or_default();
    let sqlite_code = error_code
        .map(|code| format!("{code:?}"))
        .unwrap_or_else(|| "unknown".to_owned());
    let error_summary = sqlite_error_summary(error);
    anyhow!(
        "database migration failed (phase=migration-{phase}{version}, path={}, sqliteCode={sqlite_code}{checksum}, retryable={retryable}): {error_summary}. recoveryHint={recovery_hint}",
        path.display()
    )
}

fn sqlite_error_code(error: &SqliteError) -> Option<ErrorCode> {
    match error {
        SqliteError::SqliteFailure(error, _) | SqliteError::SqlInputError { error, .. } => {
            Some(error.code)
        }
        _ => None,
    }
}

fn is_busy_or_locked(error: &SqliteError) -> bool {
    matches!(
        sqlite_error_code(error),
        Some(ErrorCode::DatabaseBusy | ErrorCode::DatabaseLocked)
    )
}

fn sqlite_error_summary(error: &SqliteError) -> String {
    match error {
        SqliteError::SqlInputError { msg, offset, .. } => {
            format!("{msg} at byte offset {offset}")
        }
        _ => error.to_string(),
    }
}

fn sha256(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

#[cfg(test)]
mod tests {
    use std::{sync::Barrier, thread};

    use super::*;

    fn write_migration(directory: &Path, filename: &str, sql: &str) {
        fs::write(directory.join(filename), sql).expect("write migration");
    }

    fn bootstrap(directory: &Path) {
        write_migration(
            directory,
            BOOTSTRAP_FILENAME,
            "CREATE TABLE example (id INTEGER PRIMARY KEY, value TEXT NOT NULL);",
        );
    }

    #[test]
    fn applies_bootstrap_idempotently_and_preserves_data() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let migrations = directory.path().join("migrations");
        fs::create_dir(&migrations).expect("create migrations");
        bootstrap(&migrations);
        let database = directory.path().join("nested").join("data.db");

        let first = migrate(&database, &migrations).expect("apply bootstrap");
        assert_eq!(first.applied_now, 1);
        assert_eq!(first.current_version, 0);

        let connection = Connection::open(&database).expect("open database");
        connection
            .execute("INSERT INTO example(value) VALUES ('kept')", [])
            .expect("insert data");
        drop(connection);

        let second = migrate(&database, &migrations).expect("repeat migrations");
        assert_eq!(second.applied_now, 0);
        let connection = Connection::open(database).expect("reopen database");
        let value: String = connection
            .query_row("SELECT value FROM example", [], |row| row.get(0))
            .expect("read preserved row");
        assert_eq!(value, "kept");
        let recorded: i64 = connection
            .query_row("SELECT COUNT(*) FROM _lite_mcp_migrations", [], |row| {
                row.get(0)
            })
            .expect("count migrations");
        assert_eq!(recorded, 1);
    }

    #[test]
    fn applies_multiple_migrations_in_numeric_order() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let migrations = directory.path().join("migrations");
        fs::create_dir(&migrations).expect("create migrations");
        bootstrap(&migrations);
        write_migration(
            &migrations,
            "0001_add_status.sql",
            "ALTER TABLE example ADD COLUMN status TEXT NOT NULL DEFAULT 'open';",
        );
        write_migration(
            &migrations,
            "0002_seed_status.sql",
            "INSERT INTO example(value, status) VALUES ('ordered', 'done');",
        );

        let report = migrate(&directory.path().join("data.db"), &migrations)
            .expect("apply ordered migrations");
        assert_eq!(report.applied_now, 3);
        assert_eq!(report.current_version, 2);
    }

    #[test]
    fn validates_migrations_in_memory_without_creating_a_database_file() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let migrations = directory.path().join("migrations");
        fs::create_dir(&migrations).expect("create migrations");
        bootstrap(&migrations);
        write_migration(
            &migrations,
            "0001_add_status.sql",
            "ALTER TABLE example ADD COLUMN status TEXT;",
        );
        let configured_database = directory.path().join("data.db");

        let report =
            validate_migrations(&configured_database, &migrations).expect("validate in memory");

        assert_eq!(report.total, 2);
        assert_eq!(report.current_version, 1);
        assert_eq!(report.pending, 2);
        assert!(!report.database_exists);
        assert!(!configured_database.exists());

        write_migration(
            &migrations,
            "0002_forbidden.sql",
            "PRAGMA user_version = 2;",
        );
        assert!(validate_migrations(&configured_database, &migrations).is_err());
        assert!(!configured_database.exists());
    }

    #[test]
    fn validates_existing_database_without_creating_or_changing_sidecars() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let migrations = directory.path().join("migrations");
        fs::create_dir(&migrations).expect("create migrations");
        bootstrap(&migrations);
        let database = directory.path().join("data.db");
        migrate(&database, &migrations).expect("apply bootstrap");
        let original = fingerprint_database_files(&database).expect("fingerprint database");
        assert_eq!(original.len(), 1, "test database should have no sidecars");

        let report =
            validate_migrations(&database, &migrations).expect("validate database snapshot");

        assert!(report.database_exists);
        assert_eq!(report.pending, 0);
        assert_eq!(
            fingerprint_database_files(&database).expect("fingerprint after validation"),
            original
        );

        fs::write(
            migrations.join(BOOTSTRAP_FILENAME),
            "CREATE TABLE changed(value INTEGER);\n",
        )
        .expect("change applied migration");
        assert!(validate_migrations(&database, &migrations).is_err());
        assert_eq!(
            fingerprint_database_files(&database).expect("fingerprint after drift check"),
            original
        );
    }

    #[test]
    fn preserves_existing_wal_and_shm_files_during_validation() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let migrations = directory.path().join("migrations");
        fs::create_dir(&migrations).expect("create migrations");
        bootstrap(&migrations);
        let database = directory.path().join("data.db");
        migrate(&database, &migrations).expect("apply bootstrap");

        let connection = Connection::open(&database).expect("open database keeper");
        connection
            .execute_batch(
                "PRAGMA journal_mode = WAL;
                 PRAGMA wal_autocheckpoint = 0;
                 INSERT INTO example(value) VALUES ('in-wal');",
            )
            .expect("leave committed data in WAL");
        let before = fingerprint_database_files(&database).expect("fingerprint WAL database");
        assert_eq!(before.len(), 3, "WAL and SHM sidecars should exist");

        let report = validate_migrations(&database, &migrations)
            .expect("validate copied database with sidecars");

        assert_eq!(report.pending, 0);
        assert_eq!(
            fingerprint_database_files(&database).expect("fingerprint WAL database after check"),
            before
        );
        drop(connection);
    }

    #[test]
    fn rolls_back_all_pending_migrations_after_failure() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let migrations = directory.path().join("migrations");
        fs::create_dir(&migrations).expect("create migrations");
        bootstrap(&migrations);
        let database = directory.path().join("data.db");
        migrate(&database, &migrations).expect("apply bootstrap");
        write_migration(
            &migrations,
            "0001_broken.sql",
            "CREATE TABLE should_roll_back(value); # private_seed_value",
        );

        let error = migrate(&database, &migrations)
            .expect_err("broken migration fails")
            .to_string();
        assert!(error.contains("0001_broken.sql"));
        assert!(error.contains("retryable=false"));
        assert!(error.contains("sqliteCode=Unknown"));
        assert!(!error.contains("private_seed_value"));

        let connection = Connection::open(database).expect("open database");
        let table_count: i64 = connection
            .query_row(
                "SELECT COUNT(*) FROM sqlite_schema WHERE name = 'should_roll_back'",
                [],
                |row| row.get(0),
            )
            .expect("check rollback");
        assert_eq!(table_count, 0);
        let ledger_count: i64 = connection
            .query_row("SELECT COUNT(*) FROM _lite_mcp_migrations", [], |row| {
                row.get(0)
            })
            .expect("check ledger rollback");
        assert_eq!(ledger_count, 1);
    }

    #[test]
    fn rejects_checksum_drift_and_missing_applied_history() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let migrations = directory.path().join("migrations");
        fs::create_dir(&migrations).expect("create migrations");
        bootstrap(&migrations);
        write_migration(
            &migrations,
            "0001_add_flag.sql",
            "ALTER TABLE example ADD COLUMN flag INTEGER NOT NULL DEFAULT 0;",
        );
        let database = directory.path().join("data.db");
        migrate(&database, &migrations).expect("apply migrations");

        write_migration(
            &migrations,
            "0001_add_flag.sql",
            "ALTER TABLE example ADD COLUMN changed INTEGER;",
        );
        let changed = migrate(&database, &migrations)
            .expect_err("changed migration fails")
            .to_string();
        assert!(changed.contains("checksum changed"));

        fs::remove_file(migrations.join("0001_add_flag.sql")).expect("remove migration");
        let missing = migrate(&database, &migrations)
            .expect_err("missing migration fails")
            .to_string();
        assert!(missing.contains("is missing"));
        assert!(missing.contains("restore the original file"));
    }

    #[test]
    fn validates_bootstrap_names_gaps_and_symlinks() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let migrations = directory.path().join("migrations");
        fs::create_dir(&migrations).expect("create migrations");
        let empty = migrate(&directory.path().join("empty.db"), &migrations)
            .expect_err("missing bootstrap fails")
            .to_string();
        assert!(empty.contains(BOOTSTRAP_FILENAME));

        write_migration(&migrations, "0000_bad name.sql", "SELECT 1;");
        let invalid = migrate(&directory.path().join("invalid.db"), &migrations)
            .expect_err("invalid filename fails")
            .to_string();
        assert!(invalid.contains("invalid migration filename"));
        fs::remove_file(migrations.join("0000_bad name.sql")).expect("remove invalid");

        bootstrap(&migrations);
        write_migration(&migrations, "0002_gap.sql", "SELECT 1;");
        let gap = migrate(&directory.path().join("gap.db"), &migrations)
            .expect_err("gap fails")
            .to_string();
        assert!(gap.contains("expected 0001"));

        fs::remove_file(migrations.join("0002_gap.sql")).expect("remove gap");
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(
                migrations.join(BOOTSTRAP_FILENAME),
                migrations.join("0001_link.sql"),
            )
            .expect("create migration symlink");
            let symlink = migrate(&directory.path().join("symlink.db"), &migrations)
                .expect_err("symlink fails")
                .to_string();
            assert!(symlink.contains("not a symlink"));
        }
    }

    #[test]
    fn rejects_duplicate_versions() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let migrations = directory.path().join("migrations");
        fs::create_dir(&migrations).expect("create migrations");
        bootstrap(&migrations);
        write_migration(&migrations, "0000_duplicate.sql", "SELECT 1;");

        let error = migrate(&directory.path().join("data.db"), &migrations)
            .expect_err("duplicate versions fail")
            .to_string();
        assert!(error.contains("duplicate migration version 0000"));
        assert!(error.contains(BOOTSTRAP_FILENAME));
        assert!(error.contains("0000_duplicate.sql"));
    }

    #[test]
    fn rejects_an_incompatible_internal_ledger() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let migrations = directory.path().join("migrations");
        fs::create_dir(&migrations).expect("create migrations");
        bootstrap(&migrations);
        let database = directory.path().join("data.db");
        let connection = Connection::open(&database).expect("create database");
        connection
            .execute_batch(
                "CREATE TABLE _lite_mcp_migrations (
                    version INTEGER PRIMARY KEY,
                    filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                );",
            )
            .expect("create incompatible ledger");
        drop(connection);

        let error = migrate(&database, &migrations)
            .expect_err("incompatible ledger fails")
            .to_string();
        assert!(error.contains("incompatible schema"));
        assert!(error.contains("retryable=false"));

        let connection = Connection::open(database).expect("reopen database");
        let bootstrap_table: i64 = connection
            .query_row(
                "SELECT COUNT(*) FROM sqlite_schema WHERE name = 'example'",
                [],
                |row| row.get(0),
            )
            .expect("check bootstrap was not applied");
        assert_eq!(bootstrap_table, 0);
    }

    #[test]
    fn rejects_transaction_pragmas_external_databases_and_ledger_access() {
        let forbidden = [
            ("BEGIN;", "transaction statements"),
            ("COMMIT;", "transaction statements"),
            ("ROLLBACK;", "transaction statements"),
            ("SAVEPOINT mine;", "transaction statements"),
            ("RELEASE mine;", "transaction statements"),
            ("PRAGMA user_version = 2;", "PRAGMA statements"),
            ("ATTACH ':memory:' AS other;", "ATTACH and DETACH"),
            ("DETACH other;", "ATTACH and DETACH"),
            (
                "DELETE FROM _lite_mcp_migrations;",
                "internal migration ledger",
            ),
            (
                "CREATE TABLE trigger_target(value); \
                 CREATE TRIGGER mutate_ledger AFTER INSERT ON trigger_target BEGIN \
                   DELETE FROM _lite_mcp_migrations; \
                 END;",
                "CREATE TRIGGER is not allowed",
            ),
        ];
        for (sql, reason) in forbidden {
            let directory = tempfile::tempdir().expect("create temp directory");
            let migrations = directory.path().join("migrations");
            fs::create_dir(&migrations).expect("create migrations");
            bootstrap(&migrations);
            write_migration(&migrations, "0001_forbidden.sql", sql);

            let error = migrate(&directory.path().join("data.db"), &migrations)
                .expect_err("forbidden migration fails")
                .to_string();
            assert!(error.contains(reason), "unexpected error: {error}");
        }
    }

    #[test]
    fn rejects_oversized_and_non_utf8_migrations() {
        let oversized_directory = tempfile::tempdir().expect("create oversized directory");
        let oversized_migrations = oversized_directory.path().join("migrations");
        fs::create_dir(&oversized_migrations).expect("create oversized migrations");
        bootstrap(&oversized_migrations);
        fs::write(
            oversized_migrations.join("0001_too_large.sql"),
            vec![b' '; MAX_MIGRATION_BYTES as usize + 1],
        )
        .expect("write oversized migration");
        let oversized = migrate(
            &oversized_directory.path().join("data.db"),
            &oversized_migrations,
        )
        .expect_err("oversized migration fails")
        .to_string();
        assert!(oversized.contains("exceeds the 4194304 byte limit"));
        assert!(oversized.contains("retryable=false"));

        let invalid_directory = tempfile::tempdir().expect("create invalid UTF-8 directory");
        let invalid_migrations = invalid_directory.path().join("migrations");
        fs::create_dir(&invalid_migrations).expect("create invalid UTF-8 migrations");
        bootstrap(&invalid_migrations);
        fs::write(invalid_migrations.join("0001_invalid_utf8.sql"), [0xff])
            .expect("write invalid UTF-8 migration");
        let invalid = migrate(
            &invalid_directory.path().join("data.db"),
            &invalid_migrations,
        )
        .expect_err("invalid UTF-8 migration fails")
        .to_string();
        assert!(invalid.contains("must be UTF-8"));
        assert!(invalid.contains("retryable=false"));
    }

    #[test]
    fn preserves_legacy_metadata_and_user_data() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let migrations = directory.path().join("migrations");
        fs::create_dir(&migrations).expect("create migrations");
        bootstrap(&migrations);
        let database = directory.path().join("data.db");
        let connection = Connection::open(&database).expect("create legacy database");
        connection
            .execute_batch(
                "CREATE TABLE _lite_mcp_meta(schema_version INTEGER PRIMARY KEY);\n\
                 INSERT INTO _lite_mcp_meta VALUES (1);\n\
                 CREATE TABLE user_data(value TEXT);\n\
                 INSERT INTO user_data VALUES ('preserved');",
            )
            .expect("seed legacy database");
        drop(connection);

        migrate(&database, &migrations).expect("migrate legacy database");
        let connection = Connection::open(database).expect("open migrated database");
        let legacy: i64 = connection
            .query_row("SELECT schema_version FROM _lite_mcp_meta", [], |row| {
                row.get(0)
            })
            .expect("read legacy metadata");
        let value: String = connection
            .query_row("SELECT value FROM user_data", [], |row| row.get(0))
            .expect("read user data");
        assert_eq!(legacy, 1);
        assert_eq!(value, "preserved");
    }

    #[test]
    fn concurrent_launchers_apply_each_migration_once() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let migrations = directory.path().join("migrations");
        fs::create_dir(&migrations).expect("create migrations");
        bootstrap(&migrations);
        let database = directory.path().join("data.db");
        let barrier = Arc::new(Barrier::new(2));
        let mut threads = Vec::new();
        for _ in 0..2 {
            let database = database.clone();
            let migrations = migrations.clone();
            let barrier = Arc::clone(&barrier);
            threads.push(thread::spawn(move || {
                barrier.wait();
                migrate(&database, &migrations).expect("concurrent migration")
            }));
        }
        let reports = threads
            .into_iter()
            .map(|thread| thread.join().expect("join migration thread"))
            .collect::<Vec<_>>();
        assert_eq!(
            reports
                .iter()
                .map(|report| report.applied_now)
                .sum::<usize>(),
            1
        );
    }

    #[test]
    fn source_bootstrap_creates_the_default_dashboard_table() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let source = Path::new(env!("CARGO_MANIFEST_DIR")).join("migrations");
        let database = directory.path().join("data.db");

        migrate(&database, &source).expect("apply source migrations");
        let connection = Connection::open(database).expect("open database");
        let exists: i64 = connection
            .query_row(
                "SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name='dashboard_example_entries'",
                [],
                |row| row.get(0),
            )
            .expect("inspect default table");
        assert_eq!(exists, 1);
    }
}
