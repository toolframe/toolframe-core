use std::{
    fs::{self, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
};

use anyhow::{Context, Result, bail};

use crate::{db, tools};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ScaffoldKind {
    Migration,
    Tool,
    Widget,
}

pub fn scaffold(kind: ScaffoldKind, name: &str, root: &Path) -> Result<PathBuf> {
    require_directory(root, kind.label())?;
    let (filename, content) = match kind {
        ScaffoldKind::Migration => {
            let filename = db::next_migration_filename(root, name)?;
            let content =
                format!("-- {filename}\n-- Add migration SQL here before starting lite-mcp.\n");
            (filename, content)
        }
        ScaffoldKind::Tool => {
            validate_module_name(name)?;
            let filename = format!("{name}.py");
            let content = format!(
                "# /// script\n# requires-python = \">=3.14,<3.15\"\n# dependencies = []\n# ///\n\nfrom fastmcp.tools import tool\n\n\n@tool\ndef {name}() -> dict[str, bool]:\n    \"\"\"Describe what this tool does.\"\"\"\n    return {{\"ok\": True}}\n"
            );
            (filename, content)
        }
        ScaffoldKind::Widget => {
            validate_module_name(name)?;
            let filename = format!("{name}.py");
            let title = name
                .split('_')
                .filter(|part| !part.is_empty())
                .map(capitalize)
                .collect::<Vec<_>>()
                .join(" ");
            let content = format!(
                "\"\"\"{title} dashboard widget.\"\"\"\n\nimport streamlit as st\nfrom lite_mcp_dashboard import DashboardContext\n\nTITLE = {title:?}\n\n\ndef render(context: DashboardContext) -> None:\n    \"\"\"Render the {title} widget.\"\"\"\n    st.subheader(TITLE)\n    st.caption(f\"Workspace: {{context.workspace}}\")\n"
            );
            (filename, content)
        }
    };

    let destination = root.join(filename);
    let mut file = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&destination)
        .with_context(|| {
            format!(
                "refusing to overwrite scaffold target {}",
                destination.display()
            )
        })?;
    file.write_all(content.as_bytes())
        .with_context(|| format!("could not write scaffold {}", destination.display()))?;
    file.sync_all()
        .with_context(|| format!("could not sync scaffold {}", destination.display()))?;
    Ok(destination)
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
    if PYTHON_KEYWORDS.contains(&name) {
        bail!("scaffold name {name:?} is a Python keyword");
    }
    Ok(())
}

fn capitalize(part: &str) -> String {
    let mut characters = part.chars();
    match characters.next() {
        Some(first) => first.to_ascii_uppercase().to_string() + characters.as_str(),
        None => String::new(),
    }
}

const PYTHON_KEYWORDS: &[&str] = &[
    "and", "as", "assert", "async", "await", "break", "class", "continue", "def", "del", "elif",
    "else", "except", "finally", "for", "from", "global", "if", "import", "in", "is", "lambda",
    "nonlocal", "not", "or", "pass", "raise", "return", "try", "while", "with", "yield",
];

impl ScaffoldKind {
    const fn label(self) -> &'static str {
        match self {
            Self::Migration => "migrations",
            Self::Tool => "tools",
            Self::Widget => "widgets",
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scaffolds_each_supported_kind_without_overwriting() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let migrations = directory.path().join("migrations");
        let tools = directory.path().join("tools");
        let widgets = directory.path().join("widgets");
        fs::create_dir(&migrations).expect("create migrations");
        fs::create_dir(&tools).expect("create tools");
        fs::create_dir(&widgets).expect("create widgets");
        fs::write(
            migrations.join("0000_bootstrap.sql"),
            "CREATE TABLE sample(id INTEGER);\n",
        )
        .expect("write bootstrap");

        let migration = scaffold(ScaffoldKind::Migration, "add_status", &migrations)
            .expect("scaffold migration");
        let tool = scaffold(ScaffoldKind::Tool, "fetch_status", &tools).expect("scaffold tool");
        let widget =
            scaffold(ScaffoldKind::Widget, "status_card", &widgets).expect("scaffold widget");

        assert_eq!(migration.file_name().unwrap(), "0001_add_status.sql");
        assert!(
            fs::read_to_string(tool)
                .unwrap()
                .contains("def fetch_status()")
        );
        assert!(
            fs::read_to_string(widget)
                .unwrap()
                .contains("TITLE = \"Status Card\"")
        );
        assert!(scaffold(ScaffoldKind::Tool, "fetch_status", &tools).is_err());
    }

    #[test]
    fn rejects_unsafe_names_and_missing_directories() {
        let directory = tempfile::tempdir().expect("create temp directory");
        assert!(scaffold(ScaffoldKind::Tool, "Class", directory.path()).is_err());
        assert!(scaffold(ScaffoldKind::Widget, "class", directory.path()).is_err());
        assert!(
            scaffold(
                ScaffoldKind::Migration,
                "bad name",
                &directory.path().join("missing")
            )
            .is_err()
        );
    }
}
