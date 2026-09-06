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

use crate::domains;

const PYTHON_VERSION: &str = "3.14.0";
const PYTHON_REQUIREMENT: &str = ">=3.14,<3.15";
const CORE_DEPENDENCIES: &[&str] = &[
    "anyio>=4.10,<5",
    // 49.0.0 and newer no longer publish an Intel macOS wheel. Keep the
    // runtime installable on supported Monterey-era Intel machines.
    "cryptography==48.0.1",
    "fastapi==0.135.2",
    "fastapi-cordis==0.1.0",
    "fastmcp==4.0.0",
    "pydantic==2.12.5",
    "streamlit==1.61.0",
    "uvicorn==0.42.0",
];
const APP_PROJECT_NAME: &str = "toolframe-app";
const APP_PROJECT_VERSION: &str = "0.0.0";

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ToolManifest {
    pub version: u8,
    pub python_version: &'static str,
    pub domains_dir: PathBuf,
    pub tools: Vec<DiscoveredTool>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DiscoveredTool {
    pub name: String,
    pub domain: String,
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
pub struct AppManifest {
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
struct AppMetadata {
    project: AppProjectMetadata,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "kebab-case")]
struct AppProjectMetadata {
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

/// Discover every public direct tool across all complete business domains.
pub fn discover(domains_dir: &Path) -> Result<ToolCatalog> {
    let discovered_domains = domains::discover(domains_dir)
        .with_context(|| format!("could not discover tools below {}", domains_dir.display()))?;
    let mut paths = Vec::new();
    for domain in discovered_domains {
        paths.extend(discover_tool_paths(&domain.tools, &domain.name)?);
    }
    let domains_dir = domains_dir
        .canonicalize()
        .unwrap_or_else(|_| domains_dir.to_path_buf());
    build_catalog(domains_dir, paths)
}

fn discover_tool_paths(tools_dir: &Path, domain: &str) -> Result<Vec<(String, PathBuf)>> {
    let metadata = fs::symlink_metadata(tools_dir)
        .with_context(|| format!("could not inspect tools directory {}", tools_dir.display()))?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        bail!(
            "tools directory {} must be a real directory, not a symlink",
            tools_dir.display()
        );
    }
    let mut entries = fs::read_dir(tools_dir)
        .with_context(|| format!("could not scan tools directory {}", tools_dir.display()))?
        .collect::<std::io::Result<Vec<_>>>()?;
    entries.sort_by_key(fs::DirEntry::file_name);

    let mut paths = Vec::new();
    for entry in entries {
        let path = entry.path();
        let filename = path
            .file_name()
            .and_then(|name| name.to_str())
            .ok_or_else(|| anyhow!("{} [filename]: is not valid UTF-8", path.display()))?;
        if filename.starts_with(['.', '_']) || path.extension().is_none_or(|ext| ext != "py") {
            continue;
        }
        let file_type = entry
            .file_type()
            .with_context(|| format!("{} [metadata]", path.display()))?;
        if file_type.is_symlink() || !file_type.is_file() {
            bail!(
                "{} [path]: public tool must be a direct regular file, not a symlink or directory",
                path.display()
            );
        }
        paths.push((domain.to_owned(), path));
    }
    Ok(paths)
}

fn build_catalog(domains_dir: PathBuf, paths: Vec<(String, PathBuf)>) -> Result<ToolCatalog> {
    let python = Version::from_str(PYTHON_VERSION).expect("static Python version is valid");
    let mut failures = Vec::new();
    let mut tools = Vec::new();
    let mut names = BTreeSet::new();
    let mut dependencies = CORE_DEPENDENCIES
        .iter()
        .map(|dependency| (*dependency).to_owned())
        .collect::<BTreeSet<_>>();

    for (domain, path) in paths {
        match inspect_tool(&path, &domain, &python) {
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

    Ok(ToolCatalog {
        manifest: ToolManifest {
            version: 2,
            python_version: PYTHON_VERSION,
            domains_dir,
            tools,
        },
        dependencies: dependencies.into_iter().collect(),
    })
}

pub fn inspect_app_manifest(path: &Path) -> Result<AppManifest> {
    let source = fs::read_to_string(path)
        .with_context(|| format!("could not read app manifest {}", path.display()))?;
    let metadata: AppMetadata = toml::from_str(&source)
        .map_err(|error| anyhow!("invalid app manifest {}: {error}", path.display()))?;

    if metadata.project.name != APP_PROJECT_NAME {
        bail!(
            "app manifest {} [project.name]: must be {APP_PROJECT_NAME:?}",
            path.display()
        );
    }
    if metadata.project.version != APP_PROJECT_VERSION {
        bail!(
            "app manifest {} [project.version]: must be {APP_PROJECT_VERSION:?}",
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
            "app manifest {} [project.dependencies]: entry {index} must not be empty",
            path.display()
        );
    }

    let specifiers =
        VersionSpecifiers::from_str(&metadata.project.requires_python).map_err(|error| {
            anyhow!(
                "app manifest {} [project.requires-python]: invalid specifier: {error}",
                path.display()
            )
        })?;
    let python = Version::from_str(PYTHON_VERSION).expect("static Python version is valid");
    if !specifiers.contains(&python) {
        bail!(
            "app manifest {} [project.requires-python]: {:?} does not accept Python {PYTHON_VERSION}",
            path.display(),
            metadata.project.requires_python
        );
    }

    Ok(AppManifest {
        requires_python: metadata.project.requires_python,
        dependencies: metadata.project.dependencies,
    })
}

pub fn runtime_dependencies(catalog: &ToolCatalog, app: &AppManifest) -> Vec<String> {
    catalog
        .dependencies
        .iter()
        .chain(&app.dependencies)
        .cloned()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect()
}

pub fn runtime_project_toml(catalog: &ToolCatalog, app: &AppManifest) -> Result<String> {
    let dependencies = runtime_dependencies(catalog, app);
    toml::to_string_pretty(&RuntimeProject {
        project: ProjectMetadata {
            name: "toolframe-runtime",
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

fn inspect_tool(path: &Path, domain: &str, python: &Version) -> Result<DiscoveredTool> {
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
        domain: domain.to_owned(),
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

    fn write_app_manifest(directory: &Path, project_fields: &str) -> PathBuf {
        let path = directory.join("pyproject.toml");
        fs::write(&path, format!("[project]\n{project_fields}")).expect("write app manifest");
        path
    }

    fn create_domain(root: &Path, name: &str) -> PathBuf {
        let domain = root.join(name);
        fs::create_dir(&domain).expect("create domain");
        fs::write(domain.join("__init__.py"), "\n").expect("write public API");
        for child in domains::REQUIRED_DIRECTORIES {
            fs::create_dir(domain.join(child)).expect("create required directory");
        }
        domain
    }

    #[test]
    fn discovers_domain_tools_with_global_identity_and_dependencies() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let first = create_domain(directory.path(), "entry_register");
        let second = create_domain(directory.path(), "daily_reporting");
        write_tool(
            &first.join("tools"),
            "entry_register_list_entries.py",
            "# requires-python = \">=3.14,<3.15\"\n# dependencies = [\"polars==1.0.0\"]\n",
        );
        write_tool(
            &second.join("tools"),
            "daily_reporting_get_summary.py",
            "# requires-python = \">=3.14,<3.15\"\n# dependencies = []\n",
        );

        let catalog = discover(directory.path()).expect("discover domain tools");

        assert_eq!(catalog.manifest.version, 2);
        assert_eq!(
            catalog.manifest.domains_dir,
            directory
                .path()
                .canonicalize()
                .expect("canonicalize domains")
        );
        assert_eq!(catalog.manifest.tools.len(), 2);
        assert_eq!(catalog.manifest.tools[0].domain, "daily_reporting");
        assert_eq!(catalog.manifest.tools[1].domain, "entry_register");
        assert!(
            catalog
                .dependencies
                .iter()
                .any(|item| item == "polars==1.0.0")
        );
    }

    #[test]
    fn rejects_duplicate_linked_or_invalid_public_tools() {
        let duplicate = tempfile::tempdir().expect("create duplicate root");
        let first = create_domain(duplicate.path(), "entry_register");
        let second = create_domain(duplicate.path(), "daily_reporting");
        for domain in [&first, &second] {
            write_tool(
                &domain.join("tools"),
                "list_entries.py",
                "# requires-python = \">=3.14,<3.15\"\n# dependencies = []\n",
            );
        }
        let error = discover(duplicate.path())
            .expect_err("duplicate tool name fails")
            .to_string();
        assert!(error.contains("duplicate tool name list_entries"));

        let incompatible = tempfile::tempdir().expect("create incompatible root");
        let domain = create_domain(incompatible.path(), "entry_register");
        write_tool(
            &domain.join("tools"),
            "future.py",
            "# requires-python = \">=3.15\"\n# dependencies = []\n",
        );
        assert!(
            discover(incompatible.path())
                .unwrap_err()
                .to_string()
                .contains("Python 3.14.0")
        );

        #[cfg(unix)]
        {
            let linked = tempfile::tempdir().expect("create linked root");
            let domain = create_domain(linked.path(), "entry_register");
            let target = linked.path().join("_target.py");
            write_tool(
                linked.path(),
                "_target.py",
                "# requires-python = \">=3.14,<3.15\"\n# dependencies = []\n",
            );
            std::os::unix::fs::symlink(
                target,
                domain.join("tools").join("entry_register_list_entries.py"),
            )
            .expect("create public tool symlink");
            let error = discover(linked.path())
                .expect_err("public tool symlink fails")
                .to_string();
            assert!(error.contains("public tool must be a direct regular file"));
        }
    }

    #[test]
    fn rejects_tool_uv_configuration_and_extracts_pep_723_comments() {
        let directory = tempfile::tempdir().expect("create root");
        let domain = create_domain(directory.path(), "entry_register");
        write_tool(
            &domain.join("tools"),
            "entry_register_custom.py",
            "# requires-python = \">=3.14\"\n# dependencies = []\n# [tool.uv]\n# resolution = \"lowest-direct\"\n",
        );
        assert!(
            discover(directory.path())
                .unwrap_err()
                .to_string()
                .contains("[tool.uv]")
        );

        let metadata = extract_script_metadata(
            "#!/usr/bin/env python\n# /// script\n# requires-python = \">=3.14\"\n# dependencies = []\n# ///\n",
        )
        .expect("extract metadata");
        assert!(metadata.contains("requires-python"));
        assert!(metadata.contains("dependencies = []"));
    }

    #[test]
    fn validates_app_manifest_and_merges_dependencies_deterministically() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let domains = directory.path().join("domains");
        fs::create_dir(&domains).expect("create domains");
        let domain = create_domain(&domains, "entry_register");
        write_tool(
            &domain.join("tools"),
            "entry_register_weather.py",
            "# requires-python = \">=3.14,<3.15\"\n# dependencies = [\"httpx==1.0.0\"]\n",
        );
        let app = directory.path().join("app");
        fs::create_dir(&app).expect("create app");
        let app_path = write_app_manifest(
            &app,
            "name = \"toolframe-app\"\nversion = \"0.0.0\"\nrequires-python = \">=3.14,<3.15\"\ndependencies = [\"duckdb==1.4.0\", \"httpx==1.0.0\"]\n",
        );

        let catalog = discover(&domains).expect("discover tools");
        let app = inspect_app_manifest(&app_path).expect("inspect app manifest");
        let dependencies = runtime_dependencies(&catalog, &app);

        assert!(dependencies.windows(2).all(|items| items[0] < items[1]));
        assert_eq!(
            dependencies
                .iter()
                .filter(|dependency| dependency.as_str() == "httpx==1.0.0")
                .count(),
            1
        );
        assert!(dependencies.iter().any(|item| item == "duckdb==1.4.0"));
        assert!(dependencies.iter().any(|item| item == "streamlit==1.61.0"));
        assert!(
            dependencies
                .iter()
                .any(|item| item == "fastapi-cordis==0.1.0")
        );

        let project = runtime_project_toml(&catalog, &app).expect("render UV project");
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
    fn rejects_unsupported_or_incompatible_app_manifest_fields() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let path = write_app_manifest(
            directory.path(),
            "name = \"toolframe-app\"\nversion = \"0.0.0\"\nrequires-python = \">=3.15\"\ndependencies = []\n",
        );
        assert!(
            inspect_app_manifest(&path)
                .unwrap_err()
                .to_string()
                .contains("does not accept Python 3.14.0")
        );

        fs::write(
            &path,
            "[project]\nname = \"toolframe-app\"\nversion = \"0.0.0\"\nrequires-python = \">=3.14\"\ndependencies = [\"   \"]\n",
        )
        .expect("replace app manifest");
        assert!(
            inspect_app_manifest(&path)
                .unwrap_err()
                .to_string()
                .contains("must not be empty")
        );

        fs::write(
            &path,
            "[project]\nname = \"toolframe-app\"\nversion = \"0.0.0\"\nrequires-python = \">=3.14\"\ndependencies = []\ndescription = \"unsupported\"\n\n[tool.uv]\npackage = false\n",
        )
        .expect("replace app manifest");
        let error = inspect_app_manifest(&path).unwrap_err().to_string();
        assert!(error.contains("unknown field"), "unexpected error: {error}");
    }
}
