use std::{
    fs::{self, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
};

use anyhow::{Context, Result, bail};

use crate::{db, domains, runtime, tools};

const DOMAIN_FILES: &[(&str, &str)] = &[
    (
        "__init__.py",
        "\"\"\"High-level public business API for this domain.\"\"\"\n",
    ),
    (
        "llms/capabilities.md",
        "# Capabilities\n\nDescribe only the capabilities exposed to peer agents, then link this document explicitly from `app/llms.txt`.\n",
    ),
    ("model/__init__.py", "\"\"\"Domain model.\"\"\"\n"),
    ("tools/__init__.py", "\"\"\"Domain tools.\"\"\"\n"),
    ("ui/__init__.py", "\"\"\"Domain UI widgets.\"\"\"\n"),
    (
        "migrations/.gitkeep",
        "# Keep this required domain directory until its first migration.\n",
    ),
];

pub fn scaffold_domain(domains_root: &Path, name: &str) -> Result<PathBuf> {
    require_directory(domains_root, "domains")?;
    domains::validate_name(name)?;
    let destination = domains_root.join(name);
    if !runtime::scaffold_directory_if_missing(&destination, "domain", DOMAIN_FILES)? {
        bail!(
            "refusing to overwrite existing domain {}",
            destination.display()
        );
    }
    Ok(destination)
}

pub fn scaffold_tool(domains_root: &Path, domain: &str, name: &str) -> Result<PathBuf> {
    validate_module_name(name)?;
    let selected = select_domain(domains_root, domain)?;
    let catalog = tools::discover(domains_root)?;
    if catalog.manifest.tools.iter().any(|tool| tool.name == name) {
        bail!("tool name {name:?} already exists; public tool names are global");
    }
    let content = format!(
        "# /// script\n# requires-python = \">=3.14,<3.15\"\n# dependencies = []\n# ///\n\nfrom fastmcp.tools import tool\n\n\n@tool\ndef {name}() -> dict[str, bool]:\n    \"\"\"TODO: implement one bounded business capability.\"\"\"\n    return {{\"ok\": True}}\n"
    );
    create_source_file(&selected.tools.join(format!("{name}.py")), &content)
}

pub fn scaffold_widget(domains_root: &Path, domain: &str, name: &str) -> Result<PathBuf> {
    validate_module_name(name)?;
    let selected = select_domain(domains_root, domain)?;
    let title = name
        .split('_')
        .filter(|part| !part.is_empty())
        .map(capitalize)
        .collect::<Vec<_>>()
        .join(" ");
    let content = format!(
        "\"\"\"{title} dashboard widget.\"\"\"\n\nimport streamlit as st\nfrom toolframe_dashboard import DashboardContext\n\nTITLE = {title:?}\n\n\ndef render(context: DashboardContext) -> None:\n    \"\"\"Render the {title} widget.\"\"\"\n    st.subheader(TITLE)\n    st.caption(f\"Workspace: {{context.workspace}}\")\n"
    );
    create_source_file(&selected.ui.join(format!("{name}.py")), &content)
}

pub fn scaffold_migration(domains_root: &Path, domain: &str, description: &str) -> Result<PathBuf> {
    let selected = select_domain(domains_root, domain)?;
    let filename = db::next_domain_migration_filename(domains_root, description)?;
    let content = format!(
        "-- {filename}\n-- Add migration SQL for the {domain} domain before starting toolframe.\n"
    );
    create_source_file(&selected.migrations.join(filename), &content)
}

fn select_domain(domains_root: &Path, name: &str) -> Result<domains::DomainLayout> {
    domains::validate_name(name)?;
    domains::discover(domains_root)?
        .into_iter()
        .find(|domain| domain.name == name)
        .ok_or_else(|| {
            anyhow::anyhow!(
                "domain {name:?} does not exist in {}",
                domains_root.display()
            )
        })
}

fn create_source_file(path: &Path, content: &str) -> Result<PathBuf> {
    let mut file = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(path)
        .with_context(|| format!("refusing to overwrite scaffold target {}", path.display()))?;
    file.write_all(content.as_bytes())
        .with_context(|| format!("could not write scaffold {}", path.display()))?;
    file.sync_all()
        .with_context(|| format!("could not sync scaffold {}", path.display()))?;
    Ok(path.to_path_buf())
}

fn require_directory(path: &Path, label: &str) -> Result<()> {
    let metadata = fs::symlink_metadata(path)
        .with_context(|| format!("{label} directory {} does not exist", path.display()))?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        bail!(
            "{label} directory {} must be a regular directory, not a symlink",
            path.display()
        );
    }
    Ok(())
}

fn validate_module_name(name: &str) -> Result<()> {
    tools::validate_tool_name(name)?;
    domains::validate_name(name)?;
    Ok(())
}

fn capitalize(part: &str) -> String {
    let mut characters = part.chars();
    match characters.next() {
        Some(first) => first.to_ascii_uppercase().to_string() + characters.as_str(),
        None => String::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn create_complete_domain(root: &Path, name: &str) -> PathBuf {
        let domain = root.join(name);
        fs::create_dir(&domain).expect("create domain");
        fs::write(domain.join("__init__.py"), "\n").expect("write public API");
        for child in domains::REQUIRED_DIRECTORIES {
            fs::create_dir(domain.join(child)).expect("create required directory");
        }
        domain
    }

    #[test]
    fn scaffolds_domain_tool_widget_and_global_migration_without_overwriting() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let domains_root = directory.path().join("domains");
        fs::create_dir(&domains_root).expect("create domains root");
        let existing = create_complete_domain(&domains_root, "entry_register");
        fs::write(
            existing.join("migrations/0000_bootstrap.sql"),
            "CREATE TABLE entry_register_entries(id INTEGER PRIMARY KEY) STRICT;\n",
        )
        .expect("write bootstrap");

        let domain = scaffold_domain(&domains_root, "daily_reporting").expect("scaffold domain");
        assert!(domain.join("llms/capabilities.md").is_file());
        assert!(domain.join("migrations").is_dir());

        let tool = scaffold_tool(&domains_root, "daily_reporting", "daily_reporting_summary")
            .expect("scaffold tool");
        let widget = scaffold_widget(&domains_root, "daily_reporting", "summary_card")
            .expect("scaffold widget");
        let migration = scaffold_migration(&domains_root, "daily_reporting", "add_summary")
            .expect("scaffold migration");

        assert!(
            fs::read_to_string(tool)
                .unwrap()
                .contains("def daily_reporting_summary()")
        );
        assert!(
            fs::read_to_string(widget)
                .unwrap()
                .contains("TITLE = \"Summary Card\"")
        );
        assert_eq!(migration.file_name().unwrap(), "0001_add_summary.sql");
        assert!(
            scaffold_tool(&domains_root, "daily_reporting", "daily_reporting_summary").is_err()
        );
        assert!(scaffold_domain(&domains_root, "daily_reporting").is_err());
    }

    #[test]
    fn rejects_unsafe_names_and_unknown_domains() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let domains_root = directory.path().join("domains");
        fs::create_dir(&domains_root).expect("create domains root");
        create_complete_domain(&domains_root, "entry_register");

        assert!(scaffold_domain(&domains_root, "Class").is_err());
        assert!(scaffold_widget(&domains_root, "missing", "status_card").is_err());
        assert!(scaffold_tool(&domains_root, "entry_register", "class").is_err());
    }
}
