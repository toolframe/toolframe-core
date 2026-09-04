use std::{fs, path::Path, path::PathBuf};

use anyhow::{Context, Result, anyhow, bail};

pub const REQUIRED_DIRECTORIES: [&str; 5] = ["llms", "model", "tools", "ui", "migrations"];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DomainLayout {
    pub name: String,
    pub root: PathBuf,
    pub public_api: PathBuf,
    pub llms: PathBuf,
    pub model: PathBuf,
    pub tools: PathBuf,
    pub ui: PathBuf,
    pub migrations: PathBuf,
}

/// Discover the complete, direct business domains under one source root.
///
/// Public entries are strict: a domain must be a real lower-snake-case
/// directory with one ``__init__.py`` public API and all required source
/// directories. Dot- and underscore-prefixed entries remain private and are
/// ignored.
pub fn discover(root: &Path) -> Result<Vec<DomainLayout>> {
    require_regular_directory(root, "domains root")?;
    let mut entries = fs::read_dir(root)
        .with_context(|| format!("could not read domains root {}", root.display()))?
        .collect::<std::io::Result<Vec<_>>>()
        .with_context(|| format!("could not inspect entries in {}", root.display()))?;
    entries.sort_by_key(fs::DirEntry::file_name);

    let mut domains = Vec::new();
    for entry in entries {
        let name = entry
            .file_name()
            .into_string()
            .map_err(|_| anyhow!("domains root {} contains a non-UTF-8 entry", root.display()))?;
        if name.starts_with(['.', '_']) {
            continue;
        }
        if let Err(error) = validate_name(&name) {
            bail!("invalid domain {}: {error}", entry.path().display());
        }
        let file_type = entry
            .file_type()
            .with_context(|| format!("could not inspect domain {}", entry.path().display()))?;
        if file_type.is_symlink() || !file_type.is_dir() {
            bail!(
                "domain {} must be a direct regular directory, not a symlink or file",
                entry.path().display()
            );
        }

        let domain_root = entry.path();
        let public_api = domain_root.join("__init__.py");
        require_regular_file(&public_api, "domain public API")?;
        for child in REQUIRED_DIRECTORIES {
            require_regular_directory(&domain_root.join(child), &format!("domain {name} {child}"))?;
        }
        domains.push(DomainLayout {
            name,
            public_api,
            llms: domain_root.join("llms"),
            model: domain_root.join("model"),
            tools: domain_root.join("tools"),
            ui: domain_root.join("ui"),
            migrations: domain_root.join("migrations"),
            root: domain_root,
        });
    }
    if domains.is_empty() {
        bail!(
            "domains root {} must contain at least one public business domain",
            root.display()
        );
    }
    Ok(domains)
}

pub fn validate_name(name: &str) -> Result<()> {
    let bytes = name.as_bytes();
    if bytes.is_empty()
        || !bytes[0].is_ascii_lowercase()
        || !bytes
            .iter()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'_')
        || name.ends_with('_')
        || name.contains("__")
    {
        bail!("name must be lower_snake_case and begin with a letter");
    }
    if matches!(
        name,
        "and"
            | "as"
            | "assert"
            | "async"
            | "await"
            | "break"
            | "class"
            | "continue"
            | "def"
            | "del"
            | "elif"
            | "else"
            | "except"
            | "finally"
            | "for"
            | "from"
            | "global"
            | "if"
            | "import"
            | "in"
            | "is"
            | "lambda"
            | "nonlocal"
            | "not"
            | "or"
            | "pass"
            | "raise"
            | "return"
            | "try"
            | "while"
            | "with"
            | "yield"
    ) {
        bail!("name must not be a Python keyword");
    }
    Ok(())
}

fn require_regular_directory(path: &Path, label: &str) -> Result<()> {
    let metadata = fs::symlink_metadata(path)
        .with_context(|| format!("could not inspect {label} directory {}", path.display()))?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        bail!(
            "{label} directory {} must be a real directory, not a symlink",
            path.display()
        );
    }
    Ok(())
}

fn require_regular_file(path: &Path, label: &str) -> Result<()> {
    let metadata = fs::symlink_metadata(path)
        .with_context(|| format!("could not inspect {label} file {}", path.display()))?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        bail!(
            "{label} file {} must be a regular file, not a symlink or directory",
            path.display()
        );
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn complete_domain(root: &Path, name: &str) -> PathBuf {
        let domain = root.join(name);
        fs::create_dir(&domain).expect("create domain");
        fs::write(domain.join("__init__.py"), "\n").expect("write public API");
        for child in REQUIRED_DIRECTORIES {
            fs::create_dir(domain.join(child)).expect("create required directory");
        }
        domain
    }

    #[test]
    fn discovers_complete_public_domains_in_name_order() {
        let temporary = tempfile::tempdir().expect("create temporary directory");
        complete_domain(temporary.path(), "zebra_care");
        complete_domain(temporary.path(), "entry_register");
        fs::write(temporary.path().join("_notes.py"), "\n").expect("write private file");

        let domains = discover(temporary.path()).expect("discover domains");

        assert_eq!(
            domains
                .iter()
                .map(|domain| domain.name.as_str())
                .collect::<Vec<_>>(),
            ["entry_register", "zebra_care"]
        );
    }

    #[test]
    fn rejects_missing_children_invalid_names_and_symlinks() {
        let incomplete = tempfile::tempdir().expect("create incomplete root");
        let domain = complete_domain(incomplete.path(), "entry_register");
        fs::remove_dir(domain.join("ui")).expect("remove ui directory");
        let error = discover(incomplete.path())
            .expect_err("missing required child fails")
            .to_string();
        assert!(error.contains("entry_register ui"));

        let invalid = tempfile::tempdir().expect("create invalid root");
        fs::create_dir(invalid.path().join("EntryRegister")).expect("create invalid domain");
        let error = discover(invalid.path())
            .expect_err("invalid name fails")
            .to_string();
        assert!(error.contains("lower_snake_case"));

        #[cfg(unix)]
        {
            let linked = tempfile::tempdir().expect("create linked root");
            let real = linked.path().join("_real");
            complete_domain(linked.path(), "_real");
            std::os::unix::fs::symlink(real, linked.path().join("entry_register"))
                .expect("create domain symlink");
            let error = discover(linked.path())
                .expect_err("linked domain fails")
                .to_string();
            assert!(error.contains("not a symlink"));
        }
    }
}
