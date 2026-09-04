use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
    str::FromStr,
};

use anyhow::{Context, Result, anyhow, bail};
use pep440_rs::{Version, VersionSpecifiers};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

const PYTHON_VERSION: &str = "3.14.0";
const PYTHON_REQUIREMENT: &str = ">=3.14,<3.15";
const CORE_DEPENDENCIES: &[&str] = &[
    "anyio>=4.10,<5",
    // 49.0.0 and newer no longer publish an Intel macOS wheel. Keep the
    // runtime installable on supported Monterey-era Intel machines.
    "cryptography==48.0.1",
    "fastapi==0.135.2",
    "fastmcp==4.0.0",
    "pydantic==2.12.5",
    "streamlit==1.61.0",
    "uvicorn==0.42.0",
];
const DASHBOARD_PROJECT_NAME: &str = "lite-mcp-dashboard";
const DASHBOARD_PROJECT_VERSION: &str = "0.0.0";

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ToolManifest {
    pub version: u8,
    pub python_version: &'static str,
    pub tools_dir: PathBuf,
    pub tools: Vec<DiscoveredTool>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DiscoveredTool {
    pub name: String,
    pub path: PathBuf,
    pub source_filename: String,
    pub source_sha256: String,
    pub dependencies: Vec<String>,
    pub requires_python: String,
}

#[derive(Debug, Clone)]
pub struct ToolCatalog {
    pub manifest: ToolManifest,
    pub dependencies: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct DashboardManifest {
    pub requires_python: String,
    pub dependencies: Vec<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "kebab-case")]
struct ScriptMetadata {
    requires_python: String,
    dependencies: Vec<String>,
    tool: Option<toml::Table>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct DashboardMetadata {
    project: DashboardProjectMetadata,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "kebab-case")]
struct DashboardProjectMetadata {
    name: String,
    version: String,
    requires_python: String,
    dependencies: Vec<String>,
}

#[derive(Debug, Serialize)]
struct RuntimeProject<'a> {
    project: ProjectMetadata<'a>,
    tool: RuntimeToolConfig,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "kebab-case")]
struct ProjectMetadata<'a> {
    name: &'a str,
    version: &'a str,
    requires_python: &'a str,
    dependencies: &'a [String],
}

#[derive(Debug, Serialize)]
struct RuntimeToolConfig {
    uv: UvConfig,
}

#[derive(Debug, Serialize)]
struct UvConfig {
    package: bool,
}

pub fn discover(tools_dir: &Path) -> Result<ToolCatalog> {
    let mut paths = fs::read_dir(tools_dir)
        .with_context(|| format!("could not scan tools directory {}", tools_dir.display()))?
        .map(|entry| entry.map(|entry| entry.path()))
        .collect::<std::io::Result<Vec<_>>>()?;
    paths.sort();

    let python = Version::from_str(PYTHON_VERSION).expect("static Python version is valid");
    let mut failures = Vec::new();
    let mut tools = Vec::new();
    let mut names = BTreeSet::new();
    let mut dependencies = CORE_DEPENDENCIES
        .iter()
        .map(|dependency| (*dependency).to_owned())
        .collect::<BTreeSet<_>>();

    for path in paths {
        let filename = match path.file_name().and_then(|name| name.to_str()) {
            Some(filename) => filename,
            None => {
                failures.push(format!("{} [filename]: is not valid UTF-8", path.display()));
                continue;
            }
        };
        if filename.starts_with('.')
            || filename.starts_with('_')
            || path.extension().is_none_or(|ext| ext != "py")
        {
            continue;
        }

        let file_type = match fs::symlink_metadata(&path) {
            Ok(metadata) => metadata.file_type(),
            Err(error) => {
                failures.push(format!("{} [metadata]: {error}", path.display()));
                continue;
            }
        };
        if file_type.is_symlink() || !file_type.is_file() {
            continue;
        }

        match inspect_tool(&path, &python) {
            Ok(tool) => {
                if !names.insert(tool.name.clone()) {
                    failures.push(format!(
                        "{} [identity]: duplicate tool name {}",
                        path.display(),
                        tool.name
                    ));
                    continue;
                }
                dependencies.extend(tool.dependencies.iter().cloned());
                tools.push(tool);
            }
            Err(error) => failures.push(format!("{}: {error:#}", path.display())),
        }
    }

    if !failures.is_empty() {
        bail!("tool discovery failed:\n- {}", failures.join("\n- "));
    }

    let tools_dir = tools_dir
        .canonicalize()
        .unwrap_or_else(|_| tools_dir.to_path_buf());
    Ok(ToolCatalog {
        manifest: ToolManifest {
            version: 1,
            python_version: PYTHON_VERSION,
            tools_dir,
            tools,
        },
        dependencies: dependencies.into_iter().collect(),
    })
}

pub fn inspect_dashboard_manifest(path: &Path) -> Result<DashboardManifest> {
    let source = fs::read_to_string(path)
        .with_context(|| format!("could not read dashboard manifest {}", path.display()))?;
    let metadata: DashboardMetadata = toml::from_str(&source)
        .map_err(|error| anyhow!("invalid dashboard manifest {}: {error}", path.display()))?;

    if metadata.project.name != DASHBOARD_PROJECT_NAME {
        bail!(
            "dashboard manifest {} [project.name]: must be {DASHBOARD_PROJECT_NAME:?}",
            path.display()
        );
    }
    if metadata.project.version != DASHBOARD_PROJECT_VERSION {
        bail!(
            "dashboard manifest {} [project.version]: must be {DASHBOARD_PROJECT_VERSION:?}",
            path.display()
        );
    }
    if let Some((index, _)) = metadata
        .project
        .dependencies
        .iter()
        .enumerate()
        .find(|(_, dependency)| dependency.trim().is_empty())
    {
        bail!(
            "dashboard manifest {} [project.dependencies]: entry {index} must not be empty",
            path.display()
        );
    }

    let specifiers =
        VersionSpecifiers::from_str(&metadata.project.requires_python).map_err(|error| {
            anyhow!(
                "dashboard manifest {} [project.requires-python]: invalid specifier: {error}",
                path.display()
            )
        })?;
    let python = Version::from_str(PYTHON_VERSION).expect("static Python version is valid");
    if !specifiers.contains(&python) {
        bail!(
            "dashboard manifest {} [project.requires-python]: {:?} does not accept Python {PYTHON_VERSION}",
            path.display(),
            metadata.project.requires_python
        );
    }

    Ok(DashboardManifest {
        requires_python: metadata.project.requires_python,
        dependencies: metadata.project.dependencies,
    })
}

pub fn runtime_dependencies(catalog: &ToolCatalog, dashboard: &DashboardManifest) -> Vec<String> {
    catalog
        .dependencies
        .iter()
        .chain(&dashboard.dependencies)
        .cloned()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect()
}

pub fn runtime_project_toml(
    catalog: &ToolCatalog,
    dashboard: &DashboardManifest,
) -> Result<String> {
    let dependencies = runtime_dependencies(catalog, dashboard);
    toml::to_string_pretty(&RuntimeProject {
        project: ProjectMetadata {
            name: "lite-mcp-runtime",
            version: "0.0.0",
            requires_python: PYTHON_REQUIREMENT,
            dependencies: &dependencies,
        },
        tool: RuntimeToolConfig {
            uv: UvConfig { package: false },
        },
    })
    .context("could not serialize generated uv project")
}

pub fn catalog_json(catalog: &ToolCatalog) -> Result<String> {
    serde_json::to_string_pretty(&catalog.manifest).context("could not serialize tool manifest")
}

pub fn content_hash(content: impl AsRef<[u8]>) -> String {
    format!("{:x}", Sha256::digest(content.as_ref()))
}

fn inspect_tool(path: &Path, python: &Version) -> Result<DiscoveredTool> {
    let source = fs::read_to_string(path).context("[read] could not read Python source")?;
    let metadata_toml = extract_script_metadata(&source).context("[metadata]")?;
    let metadata: ScriptMetadata = toml::from_str(&metadata_toml)
        .map_err(|error| anyhow!("[metadata]: invalid PEP 723 TOML: {error}"))?;

    if metadata
        .tool
        .as_ref()
        .is_some_and(|tool| tool.contains_key("uv"))
    {
        bail!("[metadata]: [tool.uv] is not supported in the shared MVP runtime");
    }
    if metadata
        .dependencies
        .iter()
        .any(|item| item.trim().is_empty())
    {
        bail!("[metadata]: dependencies must not contain empty entries");
    }

    let specifiers = VersionSpecifiers::from_str(&metadata.requires_python)
        .map_err(|error| anyhow!("[python]: invalid requires-python: {error}"))?;
    if !specifiers.contains(python) {
        bail!(
            "[python]: requires-python {:?} does not accept Python {PYTHON_VERSION}",
            metadata.requires_python
        );
    }

    let name = path
        .file_stem()
        .and_then(|stem| stem.to_str())
        .ok_or_else(|| anyhow!("[identity]: filename is not valid UTF-8"))?;
    validate_tool_name(name)?;
    let path = path.canonicalize().unwrap_or_else(|_| path.to_path_buf());

    Ok(DiscoveredTool {
        name: name.to_owned(),
        source_filename: path
            .file_name()
            .and_then(|name| name.to_str())
            .expect("validated filename")
            .to_owned(),
        path,
        source_sha256: content_hash(source),
        dependencies: metadata.dependencies,
        requires_python: metadata.requires_python,
    })
}

fn extract_script_metadata(source: &str) -> Result<String> {
    let mut lines = source.lines();
    let mut found = false;
    let mut content = Vec::new();

    for line in lines.by_ref() {
        if line.trim() == "# /// script" {
            found = true;
            break;
        }
    }
    if !found {
        bail!("missing # /// script metadata block");
    }

    for line in lines {
        if line.trim() == "# ///" {
            return Ok(content.join("\n"));
        }
        let comment = line
            .strip_prefix('#')
            .ok_or_else(|| anyhow!("metadata lines must start with #"))?;
        content.push(comment.strip_prefix(' ').unwrap_or(comment));
    }

    bail!("unterminated # /// script metadata block")
}

pub fn validate_tool_name(name: &str) -> Result<()> {
    let mut characters = name.chars();
    if name.len() > 64
        || !characters
            .next()
            .is_some_and(|character| character.is_ascii_lowercase())
        || !characters.all(|character| {
            character.is_ascii_lowercase() || character.is_ascii_digit() || character == '_'
        })
    {
        bail!(
            "[identity]: tool filename stem must be 1-64 characters of lowercase snake_case and start with a letter"
        );
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write_tool(directory: &Path, name: &str, metadata: &str) -> PathBuf {
        let path = directory.join(name);
        fs::write(
            &path,
            format!("# /// script\n{metadata}# ///\n\nprint('ok')\n"),
        )
        .expect("write tool");
        path
    }

    fn write_dashboard_manifest(directory: &Path, project_fields: &str) -> PathBuf {
        let path = directory.join("pyproject.toml");
        fs::write(&path, format!("[project]\n{project_fields}")).expect("write dashboard manifest");
        path
    }

    #[test]
    fn discovers_direct_tools_and_aggregates_dependencies() {
        let directory = tempfile::tempdir().expect("create temp directory");
        write_tool(
            directory.path(),
            "weather.py",
            "# requires-python = \">=3.14,<3.15\"\n# dependencies = [\"httpx==1.0.0\"]\n",
        );
        write_tool(
            directory.path(),
            "_helper.py",
            "# requires-python = \">=3.14\"\n# dependencies = []\n",
        );
        let nested = directory.path().join("nested");
        fs::create_dir(&nested).expect("create nested directory");
        write_tool(
            &nested,
            "nested_tool.py",
            "# requires-python = \">=3.14\"\n# dependencies = []\n",
        );

        #[cfg(unix)]
        std::os::unix::fs::symlink(
            directory.path().join("weather.py"),
            directory.path().join("linked.py"),
        )
        .expect("create tool symlink");

        let catalog = discover(directory.path()).expect("discover tools");
        assert_eq!(catalog.manifest.tools.len(), 1);
        assert_eq!(catalog.manifest.tools[0].name, "weather");
        assert!(
            catalog
                .dependencies
                .iter()
                .any(|item| item == "httpx==1.0.0")
        );
        assert!(
            catalog
                .dependencies
                .iter()
                .any(|item| item == "fastmcp==4.0.0")
        );
    }

    #[test]
    fn rejects_incompatible_python_and_tool_uv_configuration() {
        let directory = tempfile::tempdir().expect("create temp directory");
        write_tool(
            directory.path(),
            "future.py",
            "# requires-python = \">=3.15\"\n# dependencies = []\n",
        );
        assert!(
            discover(directory.path())
                .unwrap_err()
                .to_string()
                .contains("Python 3.14.0")
        );

        fs::remove_file(directory.path().join("future.py")).expect("remove incompatible tool");
        write_tool(
            directory.path(),
            "custom.py",
            "# requires-python = \">=3.14\"\n# dependencies = []\n# [tool.uv]\n# resolution = \"lowest-direct\"\n",
        );
        assert!(
            discover(directory.path())
                .unwrap_err()
                .to_string()
                .contains("[tool.uv]")
        );
    }

    #[test]
    fn extracts_pep_723_comments() {
        let metadata = extract_script_metadata(
            "#!/usr/bin/env python\n# /// script\n# requires-python = \">=3.14\"\n# dependencies = []\n# ///\n",
        )
        .expect("extract metadata");
        assert!(metadata.contains("requires-python"));
        assert!(metadata.contains("dependencies = []"));
    }

    #[test]
    fn validates_dashboard_manifest_and_merges_dependencies_deterministically() {
        let directory = tempfile::tempdir().expect("create temp directory");
        write_tool(
            directory.path(),
            "weather.py",
            "# requires-python = \">=3.14,<3.15\"\n# dependencies = [\"httpx==1.0.0\"]\n",
        );
        let dashboard_path = write_dashboard_manifest(
            directory.path(),
            "name = \"lite-mcp-dashboard\"\nversion = \"0.0.0\"\nrequires-python = \">=3.14,<3.15\"\ndependencies = [\"duckdb==1.4.0\", \"httpx==1.0.0\"]\n",
        );

        let catalog = discover(directory.path()).expect("discover tools");
        let dashboard =
            inspect_dashboard_manifest(&dashboard_path).expect("inspect dashboard manifest");
        let dependencies = runtime_dependencies(&catalog, &dashboard);

        assert!(dependencies.windows(2).all(|items| items[0] < items[1]));
        assert_eq!(
            dependencies
                .iter()
                .filter(|dependency| dependency.as_str() == "httpx==1.0.0")
                .count(),
            1
        );
        assert!(dependencies.iter().any(|item| item == "duckdb==1.4.0"));
        assert!(
            dependencies
                .iter()
                .any(|item| item == "cryptography==48.0.1")
        );
        assert!(dependencies.iter().any(|item| item == "streamlit==1.61.0"));

        let project = runtime_project_toml(&catalog, &dashboard).expect("render UV project");
        let parsed: toml::Value = toml::from_str(&project).expect("parse generated UV project");
        let rendered = parsed["project"]["dependencies"]
            .as_array()
            .expect("dependency array")
            .iter()
            .map(|item| item.as_str().expect("dependency string"))
            .collect::<Vec<_>>();
        assert_eq!(
            rendered,
            dependencies.iter().map(String::as_str).collect::<Vec<_>>()
        );
    }

    #[test]
    fn rejects_unsupported_or_incompatible_dashboard_manifest_fields() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let path = write_dashboard_manifest(
            directory.path(),
            "name = \"lite-mcp-dashboard\"\nversion = \"0.0.0\"\nrequires-python = \">=3.15\"\ndependencies = []\n",
        );
        assert!(
            inspect_dashboard_manifest(&path)
                .unwrap_err()
                .to_string()
                .contains("does not accept Python 3.14.0")
        );

        fs::write(
            &path,
            "[project]\nname = \"lite-mcp-dashboard\"\nversion = \"0.0.0\"\nrequires-python = \">=3.14\"\ndependencies = [\"   \"]\n",
        )
        .expect("replace dashboard manifest");
        assert!(
            inspect_dashboard_manifest(&path)
                .unwrap_err()
                .to_string()
                .contains("must not be empty")
        );

        fs::write(
            &path,
            "[project]\nname = \"lite-mcp-dashboard\"\nversion = \"0.0.0\"\nrequires-python = \">=3.14\"\ndependencies = []\ndescription = \"unsupported\"\n\n[tool.uv]\npackage = false\n",
        )
        .expect("replace dashboard manifest");
        let error = inspect_dashboard_manifest(&path).unwrap_err().to_string();
        assert!(error.contains("unknown field"), "unexpected error: {error}");
    }
}
