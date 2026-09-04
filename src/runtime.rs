use std::{
    collections::BTreeSet,
    env, fs,
    fs::OpenOptions,
    io::Write,
    net::{IpAddr, Ipv4Addr},
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
};

use anyhow::{Context, Result, anyhow, bail};

use crate::tools::{
    DashboardManifest, ToolCatalog, catalog_json, content_hash, runtime_dependencies,
    runtime_project_toml,
};

const CORE_SERVER: &str = include_str!("../core/server.py");
const CORE_SUPPORT: &str = include_str!("../core/toolframe_runtime.py");
const CORE_DASHBOARD: &str = include_str!("../core/toolframe_dashboard.py");
const CORE_DATABASE: &str = include_str!("../core/toolframe_db.py");
const CORE_IDEMPOTENCY: &str = include_str!("../core/toolframe_idempotency.py");
const CORE_ARTIFACTS: &str = include_str!("../core/toolframe_artifacts.py");
const CORE_DASHBOARD_RUNNER: &str = include_str!("../core/toolframe_dashboard_runner.py");
const CORE_LOGGING: &str = include_str!("../core/toolframe_logging.py");
const CORE_CLI: &str = include_str!("../core/toolframe_cli.py");
const INFO_PAGE: &str = include_str!("../static/index.html");
const DEFAULT_SQL_TOOL: &str = include_str!("../tools/execute_sql.py");
const ARTIFACT_TOOL_EXAMPLE: &str = include_str!("../tools/_artifact_example.py");
const DASHBOARD_APP: &str = include_str!("../dashboard/app.py");
const DASHBOARD_PROJECT: &str = include_str!("../dashboard/pyproject.toml");
const DASHBOARD_COMPONENTS_INIT: &str = include_str!("../dashboard/components/__init__.py");
const DASHBOARD_WIDGETS_INIT: &str = include_str!("../dashboard/widgets/__init__.py");
const DASHBOARD_EXAMPLE_CARD: &str = include_str!("../dashboard/widgets/example_card.py");
const DEFAULT_BOOTSTRAP_MIGRATION: &str = include_str!("../migrations/0000_bootstrap.sql");
const DEFAULT_MIGRATION_README: &str = include_str!("../migrations/README.md");
const DEFAULT_LLMS_FILE: &str = include_str!("../llms/llms.txt");
const EMBEDDED_LLMS_DETAIL_FILES: &[(&str, &str)] = &[
    ("agent-guide.md", include_str!("../llms/agent-guide.md")),
    ("capabilities.md", include_str!("../llms/capabilities.md")),
    ("context.md", include_str!("../llms/context.md")),
    ("workflow.md", include_str!("../llms/workflow.md")),
    ("operations.md", include_str!("../llms/operations.md")),
];
const EMBEDDED_LLMS_TREE: &[(&str, &str)] = &[
    ("llms.txt", include_str!("../llms/llms.txt")),
    ("agent-guide.md", include_str!("../llms/agent-guide.md")),
    ("capabilities.md", include_str!("../llms/capabilities.md")),
    ("context.md", include_str!("../llms/context.md")),
    ("workflow.md", include_str!("../llms/workflow.md")),
    ("operations.md", include_str!("../llms/operations.md")),
];
static STAGING_SEQUENCE: AtomicU64 = AtomicU64::new(0);

const EMBEDDED_BUSINESS_SKILL_FILES: &[(&str, &[u8])] = &[
    (
        "SKILL.md",
        include_bytes!("../skills/toolframe-business-app/SKILL.md"),
    ),
    (
        "agents/openai.yaml",
        include_bytes!("../skills/toolframe-business-app/agents/openai.yaml"),
    ),
    (
        "assets/APP_CONTEXT.template.md",
        include_bytes!("../skills/toolframe-business-app/assets/APP_CONTEXT.template.md"),
    ),
    (
        "assets/DOMAIN_MODEL.template.md",
        include_bytes!("../skills/toolframe-business-app/assets/DOMAIN_MODEL.template.md"),
    ),
    (
        "assets/MCP_UNAVAILABLE_ISSUE.template.md",
        include_bytes!("../skills/toolframe-business-app/assets/MCP_UNAVAILABLE_ISSUE.template.md"),
    ),
    (
        "references/development-handbook.md",
        include_bytes!("../skills/toolframe-business-app/references/development-handbook.md"),
    ),
    (
        "references/job-domain-model.md",
        include_bytes!("../skills/toolframe-business-app/references/job-domain-model.md"),
    ),
    (
        "references/operations-handbook.md",
        include_bytes!("../skills/toolframe-business-app/references/operations-handbook.md"),
    ),
    (
        "references/product-decisions.md",
        include_bytes!("../skills/toolframe-business-app/references/product-decisions.md"),
    ),
];

#[derive(Debug, Clone)]
pub struct AppPaths {
    pub database: PathBuf,
    pub migrations: PathBuf,
    pub tools: PathBuf,
    pub dashboard: PathBuf,
    pub workspace: PathBuf,
    pub runtime: PathBuf,
    pub llms_file: PathBuf,
    pub llms_dir: PathBuf,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PreparedDirectories {
    pub migrations_scaffolded: bool,
    pub tools_scaffolded: bool,
    pub dashboard_scaffolded: bool,
    pub llms_file_scaffolded: bool,
    pub llms_dir_scaffolded: bool,
}

#[derive(Debug, Clone)]
pub struct PreparedRuntime {
    pub core_server: PathBuf,
    pub cli_runner: PathBuf,
    pub dashboard_sdk: PathBuf,
    pub dashboard_runner: PathBuf,
    pub info_page: PathBuf,
    pub manifest: PathBuf,
    pub project: PathBuf,
    pub dependencies: Vec<String>,
}

pub fn prepare_logs_dir(root: &Path, configured: Option<&Path>) -> Result<PathBuf> {
    let path = match configured {
        Some(path) if path.is_absolute() => path.to_path_buf(),
        Some(path) => root.join(path),
        None => root.join(".toolframe").join("logs"),
    };
    fs::create_dir_all(&path)
        .with_context(|| format!("could not create logs directory {}", path.display()))?;
    if !path.is_dir() {
        bail!("logs path {} is not a directory", path.display());
    }
    path.canonicalize()
        .with_context(|| format!("could not resolve logs directory {}", path.display()))
}

pub fn executable_root() -> Result<PathBuf> {
    let executable = env::current_exe().context("could not locate the toolframe executable")?;
    executable
        .parent()
        .map(Path::to_path_buf)
        .ok_or_else(|| anyhow!("the toolframe executable has no parent directory"))
}

/// Install the bundled, business-first skill beside the executable.
///
/// The skill is deliberately a create-once artifact. A real existing target
/// is user-owned and is never inspected, completed, or upgraded. A complete
/// temporary tree is published with no-replace rename so a second launcher can
/// either observe the existing tree or safely lose the race without exposing
/// a partially written skill.
pub fn install_business_skill(executable_root: &Path) -> Result<bool> {
    let agents = executable_root.join(".agents");
    let skills = agents.join("skills");
    let target = skills.join("toolframe-business-app");

    ensure_real_directory(&agents, ".agents")?;
    ensure_real_directory(&skills, ".agents/skills")?;

    match inspect_skill_path(&target)? {
        SkillPathState::Directory => return Ok(false),
        SkillPathState::File => {
            bail!(
                "cannot install business skill: target path {} is a file; expected a real directory",
                target.display()
            );
        }
        SkillPathState::Other => {
            bail!(
                "cannot install business skill: target path {} is not a regular file or directory",
                target.display()
            );
        }
        SkillPathState::Symlink => {
            bail!(
                "cannot install business skill: target path {} is a symlink; refusing to follow it",
                target.display()
            );
        }
        SkillPathState::Missing => {}
    }

    let staging_directory = tempfile::Builder::new()
        .prefix(".toolframe-business-app-")
        .suffix(".tmp")
        .tempdir_in(&skills)
        .with_context(|| {
            format!(
                "could not create a unique temporary business skill directory in {}",
                skills.display()
            )
        })?;
    let staging = staging_directory.path().to_path_buf();

    let result = (|| -> Result<bool> {
        for (relative, content) in EMBEDDED_BUSINESS_SKILL_FILES {
            let destination = staging.join(relative);
            if let Some(parent) = destination.parent() {
                fs::create_dir_all(parent).with_context(|| {
                    format!(
                        "could not create business skill directory {}",
                        parent.display()
                    )
                })?;
            }
            write_embedded_skill_file(&destination, content)?;
        }

        // Publishing only happens after every file has been written and
        // synced. The platform-specific helper uses RENAME_EXCL/
        // RENAME_NOREPLACE where available, so two launchers cannot replace
        // one another's user-owned skill directory.
        let published = rename_directory_if_absent(&staging, &target)?;
        if published {
            Ok(true)
        } else {
            match inspect_skill_path(&target)? {
                SkillPathState::Directory => Ok(false),
                SkillPathState::File => bail!(
                    "cannot install business skill: target path {} is a file after a concurrent install",
                    target.display()
                ),
                SkillPathState::Other => bail!(
                    "cannot install business skill: target path {} is not a regular file or directory after a concurrent install",
                    target.display()
                ),
                SkillPathState::Symlink => bail!(
                    "cannot install business skill: target path {} is a symlink after a concurrent install; refusing to follow it",
                    target.display()
                ),
                SkillPathState::Missing => bail!(
                    "cannot install business skill: target path {} disappeared during a concurrent install",
                    target.display()
                ),
            }
        }
    })();

    if staging.exists() {
        fs::remove_dir_all(&staging).with_context(|| {
            format!(
                "could not remove temporary business skill directory {}",
                staging.display()
            )
        })?;
    }

    result
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum SkillPathState {
    Missing,
    Directory,
    File,
    Other,
    Symlink,
}

fn inspect_skill_path(path: &Path) -> Result<SkillPathState> {
    let metadata = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(SkillPathState::Missing);
        }
        Err(error) => {
            return Err(error)
                .with_context(|| format!("could not inspect path {}", path.display()));
        }
    };

    let file_type = metadata.file_type();
    if file_type.is_symlink() {
        Ok(SkillPathState::Symlink)
    } else if metadata.is_dir() {
        Ok(SkillPathState::Directory)
    } else if metadata.is_file() {
        Ok(SkillPathState::File)
    } else {
        Ok(SkillPathState::Other)
    }
}

fn ensure_real_directory(path: &Path, label: &str) -> Result<()> {
    loop {
        match inspect_skill_path(path)? {
            SkillPathState::Directory => return Ok(()),
            SkillPathState::File => {
                bail!(
                    "cannot install business skill: {label} path {} is a file; expected a real directory",
                    path.display()
                );
            }
            SkillPathState::Other => {
                bail!(
                    "cannot install business skill: {label} path {} is not a regular directory",
                    path.display()
                );
            }
            SkillPathState::Symlink => {
                bail!(
                    "cannot install business skill: {label} path {} is a symlink; refusing to follow it",
                    path.display()
                );
            }
            SkillPathState::Missing => match fs::create_dir(path) {
                Ok(()) => return Ok(()),
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(error) => {
                    return Err(error).with_context(|| {
                        format!(
                            "could not create {label} directory {}; expected a real directory",
                            path.display()
                        )
                    });
                }
            },
        }
    }
}

fn write_embedded_skill_file(path: &Path, content: &[u8]) -> Result<()> {
    let mut file = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(path)
        .with_context(|| format!("could not create embedded skill file {}", path.display()))?;
    file.write_all(content)
        .with_context(|| format!("could not write embedded skill file {}", path.display()))?;
    file.sync_all()
        .with_context(|| format!("could not sync embedded skill file {}", path.display()))?;
    Ok(())
}

#[allow(clippy::too_many_arguments)]
pub fn resolve_paths(
    root: &Path,
    database: Option<PathBuf>,
    migrations: Option<PathBuf>,
    tools: Option<PathBuf>,
    dashboard: Option<PathBuf>,
    workspace: Option<PathBuf>,
    runtime: Option<PathBuf>,
    llms_file: Option<PathBuf>,
    llms_dir: Option<PathBuf>,
) -> Result<AppPaths> {
    let cwd = env::current_dir().context("could not read the current directory")?;
    let llms_dir_default = root.join("llms");
    Ok(AppPaths {
        database: absolute_override(database, &cwd, &root.join("data").join("data.db")),
        migrations: absolute_override(migrations, &cwd, &root.join("migrations")),
        tools: absolute_override(tools, &cwd, &root.join("tools")),
        dashboard: absolute_override(dashboard, &cwd, &root.join("dashboard")),
        workspace: absolute_override(workspace, &cwd, &root.join("workspace")),
        runtime: absolute_override(runtime, &cwd, &root.join(".toolframe")),
        llms_file: absolute_override(llms_file, &cwd, &llms_dir_default.join("llms.txt")),
        llms_dir: absolute_override(llms_dir, &cwd, &llms_dir_default),
    })
}

pub fn prepare_directories(paths: &AppPaths) -> Result<PreparedDirectories> {
    prepare_database_parent(&paths.database)?;
    let (llms_file_scaffolded, llms_dir_scaffolded) =
        prepare_llms_source(&paths.llms_file, &paths.llms_dir)?;
    let migrations_scaffolded = scaffold_migrations_if_missing(&paths.migrations)?;
    let tools_created = create_directory_if_missing(&paths.tools, "tools")?;
    create_directory_if_missing(&paths.workspace, "workspace")?;
    create_directory_if_missing(&paths.runtime, "runtime")?;

    if tools_created {
        let destination = paths.tools.join("execute_sql.py");
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&destination)
            .with_context(|| {
                format!(
                    "could not scaffold the default SQL tool at {}",
                    destination.display()
                )
            })?;
        file.write_all(DEFAULT_SQL_TOOL.as_bytes())
            .with_context(|| format!("could not write {}", destination.display()))?;
        file.sync_all()
            .with_context(|| format!("could not sync {}", destination.display()))?;

        let example_destination = paths.tools.join("_artifact_example.py");
        let mut example_file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&example_destination)
            .with_context(|| {
                format!(
                    "could not scaffold the artifact tool example at {}",
                    example_destination.display()
                )
            })?;
        example_file
            .write_all(ARTIFACT_TOOL_EXAMPLE.as_bytes())
            .with_context(|| format!("could not write {}", example_destination.display()))?;
        example_file
            .sync_all()
            .with_context(|| format!("could not sync {}", example_destination.display()))?;
    }

    let dashboard_scaffolded = scaffold_dashboard_if_missing(&paths.dashboard)?;

    Ok(PreparedDirectories {
        migrations_scaffolded,
        tools_scaffolded: tools_created,
        dashboard_scaffolded,
        llms_file_scaffolded,
        llms_dir_scaffolded,
    })
}

pub fn prepare_runtime(
    runtime_dir: &Path,
    catalog: &ToolCatalog,
    dashboard: &DashboardManifest,
) -> Result<PreparedRuntime> {
    let core_hash = content_hash(
        [
            CORE_SERVER.as_bytes(),
            CORE_SUPPORT.as_bytes(),
            CORE_DASHBOARD.as_bytes(),
            CORE_DATABASE.as_bytes(),
            CORE_IDEMPOTENCY.as_bytes(),
            CORE_ARTIFACTS.as_bytes(),
            CORE_DASHBOARD_RUNNER.as_bytes(),
            CORE_LOGGING.as_bytes(),
            CORE_CLI.as_bytes(),
            INFO_PAGE.as_bytes(),
        ]
        .concat(),
    );
    let core_dir = runtime_dir.join("core").join(core_hash);
    let manifests_dir = runtime_dir.join("manifests");
    let environments_dir = runtime_dir.join("environments");
    for directory in [&core_dir, &manifests_dir, &environments_dir] {
        fs::create_dir_all(directory)
            .with_context(|| format!("could not create {}", directory.display()))?;
    }

    let core_server = core_dir.join("server.py");
    let core_support = core_dir.join("toolframe_runtime.py");
    let dashboard_sdk = core_dir.join("toolframe_dashboard.py");
    let database_sdk = core_dir.join("toolframe_db.py");
    let idempotency = core_dir.join("toolframe_idempotency.py");
    let artifacts_sdk = core_dir.join("toolframe_artifacts.py");
    let dashboard_runner = core_dir.join("toolframe_dashboard_runner.py");
    let core_logging = core_dir.join("toolframe_logging.py");
    let cli_runner = core_dir.join("toolframe_cli.py");
    let info_page = core_dir.join("info.html");
    write_managed(&core_server, CORE_SERVER)?;
    write_managed(&core_support, CORE_SUPPORT)?;
    write_managed(&dashboard_sdk, CORE_DASHBOARD)?;
    write_managed(&database_sdk, CORE_DATABASE)?;
    write_managed(&idempotency, CORE_IDEMPOTENCY)?;
    write_managed(&artifacts_sdk, CORE_ARTIFACTS)?;
    write_managed(&dashboard_runner, CORE_DASHBOARD_RUNNER)?;
    write_managed(&core_logging, CORE_LOGGING)?;
    write_managed(&cli_runner, CORE_CLI)?;
    write_managed(&info_page, INFO_PAGE)?;

    let project_toml = runtime_project_toml(catalog, dashboard)?;
    let dependencies = runtime_dependencies(catalog, dashboard);
    let project = environments_dir.join(content_hash(&project_toml));
    fs::create_dir_all(&project)
        .with_context(|| format!("could not create UV project {}", project.display()))?;
    write_managed(&project.join("pyproject.toml"), &project_toml)?;

    let manifest_json = catalog_json(catalog)?;
    let manifest = manifests_dir.join(format!("{}.json", content_hash(&manifest_json)));
    write_managed(&manifest, &manifest_json)?;

    Ok(PreparedRuntime {
        core_server,
        cli_runner,
        dashboard_sdk,
        dashboard_runner,
        info_page,
        manifest,
        project,
        dependencies,
    })
}

pub fn allowed_hosts(bind_host: IpAddr) -> Vec<String> {
    let mut hosts = BTreeSet::from([
        "localhost".to_owned(),
        Ipv4Addr::LOCALHOST.to_string(),
        "::1".to_owned(),
    ]);

    if !bind_host.is_unspecified() {
        hosts.insert(bind_host.to_string());
    }
    if let Ok(interfaces) = local_ip_address::list_afinet_netifas() {
        hosts.extend(
            interfaces
                .into_iter()
                .map(|(_, address)| address.to_string()),
        );
    }
    hosts.into_iter().collect()
}

pub fn allowed_origins(bind_host: IpAddr, port: u16) -> Vec<String> {
    let mut origins = BTreeSet::from([
        format!("http://localhost:{port}"),
        http_origin(IpAddr::V4(Ipv4Addr::LOCALHOST), port),
        http_origin(IpAddr::V6(std::net::Ipv6Addr::LOCALHOST), port),
    ]);
    if !bind_host.is_unspecified() {
        origins.insert(http_origin(bind_host, port));
    }
    if let Ok(interfaces) = local_ip_address::list_afinet_netifas() {
        origins.extend(
            interfaces
                .into_iter()
                .map(|(_, address)| http_origin(address, port)),
        );
    }
    origins.into_iter().collect()
}

pub fn http_origin(address: IpAddr, port: u16) -> String {
    match address {
        IpAddr::V4(address) => format!("http://{address}:{port}"),
        IpAddr::V6(address) => format!("http://[{address}]:{port}"),
    }
}

fn absolute_override(value: Option<PathBuf>, cwd: &Path, default: &Path) -> PathBuf {
    match value {
        Some(path) if path.is_absolute() => path,
        Some(path) => cwd.join(path),
        None => default.to_path_buf(),
    }
}

fn create_directory_if_missing(path: &Path, label: &str) -> Result<bool> {
    if path.exists() {
        if path.is_dir() {
            return Ok(false);
        }
        bail!(
            "{label} path {} exists but is not a directory",
            path.display()
        );
    }

    fs::create_dir_all(path)
        .with_context(|| format!("could not create {label} directory {}", path.display()))?;
    Ok(true)
}

fn prepare_database_parent(path: &Path) -> Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| anyhow!("database path {} has no parent directory", path.display()))?;
    create_directory_if_missing(parent, "database parent")?;
    Ok(())
}

fn scaffold_dashboard_if_missing(path: &Path) -> Result<bool> {
    scaffold_directory_if_missing(
        path,
        "dashboard",
        &[
            ("app.py", DASHBOARD_APP),
            ("pyproject.toml", DASHBOARD_PROJECT),
            ("components/__init__.py", DASHBOARD_COMPONENTS_INIT),
            ("widgets/__init__.py", DASHBOARD_WIDGETS_INIT),
            ("widgets/example_card.py", DASHBOARD_EXAMPLE_CARD),
        ],
    )
}

fn scaffold_migrations_if_missing(path: &Path) -> Result<bool> {
    scaffold_directory_if_missing(
        path,
        "migrations",
        &[
            ("0000_bootstrap.sql", DEFAULT_BOOTSTRAP_MIGRATION),
            ("README.md", DEFAULT_MIGRATION_README),
        ],
    )
}

fn validate_llms_file_path(path: &Path) -> Result<bool> {
    match inspect_skill_path(path)? {
        SkillPathState::Missing => Ok(true),
        SkillPathState::File => Ok(false),
        SkillPathState::Directory => bail!(
            "llms file path {} is a directory; expected a regular file",
            path.display()
        ),
        SkillPathState::Other => bail!(
            "llms file path {} is not a regular file; refusing to use it",
            path.display()
        ),
        SkillPathState::Symlink => bail!(
            "llms file path {} is a symlink; refusing to follow it",
            path.display()
        ),
    }
}

fn validate_llms_directory_path(path: &Path) -> Result<bool> {
    match inspect_skill_path(path)? {
        SkillPathState::Missing => Ok(true),
        SkillPathState::Directory => Ok(false),
        SkillPathState::File => bail!(
            "llms directory path {} is a file; expected a real directory",
            path.display()
        ),
        SkillPathState::Other => bail!(
            "llms directory path {} is not a real directory",
            path.display()
        ),
        SkillPathState::Symlink => bail!(
            "llms directory path {} is a symlink; refusing to follow it",
            path.display()
        ),
    }
}

fn validate_llms_pair(file: &Path, directory: &Path) -> Result<bool> {
    let file_missing = validate_llms_file_path(file)?;
    let directory_missing = validate_llms_directory_path(directory)?;
    if file_missing != directory_missing {
        if file_missing {
            bail!(
                "llms source is partial: llms file path {} is missing while llms directory path {} exists; both must be present or both absent",
                file.display(),
                directory.display()
            );
        }
        bail!(
            "llms source is partial: llms directory path {} is missing while llms file path {} exists; both must be present or both absent",
            directory.display(),
            file.display()
        );
    }
    Ok(file_missing)
}

fn prepare_llms_source(file: &Path, directory: &Path) -> Result<(bool, bool)> {
    if file == directory.join("llms.txt") {
        let directory_missing = validate_llms_directory_path(directory)?;
        if directory_missing {
            let published = scaffold_directory_if_missing(directory, "llms", EMBEDDED_LLMS_TREE)?;
            if published {
                return Ok((true, true));
            }
        }

        if validate_llms_file_path(file)? {
            bail!(
                "llms source is partial: index file {} is missing from existing llms directory {}; the directory is user-owned and is not repaired automatically",
                file.display(),
                directory.display()
            );
        }
        validate_llms_directory_path(directory)?;
        return Ok((false, false));
    }

    let llms_scaffolded = validate_llms_pair(file, directory)?;
    let file_scaffolded = if llms_scaffolded {
        scaffold_llms_file_if_missing(file)?
    } else {
        false
    };
    let directory_scaffolded = if llms_scaffolded {
        scaffold_llms_directory_if_missing(directory)?
    } else {
        false
    };
    validate_llms_pair(file, directory)?;
    Ok((file_scaffolded, directory_scaffolded))
}

fn scaffold_llms_file_if_missing(path: &Path) -> Result<bool> {
    if !validate_llms_file_path(path)? {
        return Ok(false);
    }

    let parent = path
        .parent()
        .ok_or_else(|| anyhow!("llms file path {} has no parent directory", path.display()))?;
    fs::create_dir_all(parent).with_context(|| {
        format!(
            "could not create llms file parent directory {}",
            parent.display()
        )
    })?;

    let sequence = STAGING_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    let staging = parent.join(format!(
        ".toolframe-llms-file-{}-{sequence}.tmp",
        std::process::id()
    ));
    let mut published = false;
    let result = (|| -> Result<()> {
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&staging)
            .with_context(|| {
                format!(
                    "could not create llms file staging path {}",
                    staging.display()
                )
            })?;
        file.write_all(DEFAULT_LLMS_FILE.as_bytes())
            .with_context(|| {
                format!(
                    "could not write llms file staging path {}",
                    staging.display()
                )
            })?;
        file.sync_all().with_context(|| {
            format!(
                "could not sync llms file staging path {}",
                staging.display()
            )
        })?;
        published = rename_file_if_absent(&staging, path)?;
        Ok(())
    })();
    if staging.exists() {
        fs::remove_file(&staging).with_context(|| {
            format!(
                "could not remove llms file staging path {}",
                staging.display()
            )
        })?;
    }
    result?;
    if !published {
        // A concurrent launcher may have published the create-once file.
        // Re-inspect without following symlinks before treating the race as a
        // successful no-op.
        validate_llms_file_path(path)?;
    }
    Ok(published)
}

fn scaffold_llms_directory_if_missing(path: &Path) -> Result<bool> {
    if !validate_llms_directory_path(path)? {
        return Ok(false);
    }

    let published = scaffold_directory_if_missing(path, "llms", EMBEDDED_LLMS_DETAIL_FILES)?;
    if !published {
        // The directory may have been created by another launcher while the
        // complete staged tree was being written. Confirm its type without
        // following a symlink before preserving it.
        validate_llms_directory_path(path)?;
    }
    Ok(published)
}

fn scaffold_directory_if_missing(path: &Path, label: &str, files: &[(&str, &str)]) -> Result<bool> {
    if path.exists() {
        if path.is_dir() {
            return Ok(false);
        }
        bail!(
            "{label} path {} exists but is not a directory",
            path.display()
        );
    }

    let parent = path
        .parent()
        .ok_or_else(|| anyhow!("{label} path {} has no parent directory", path.display()))?;
    fs::create_dir_all(parent).with_context(|| {
        format!(
            "could not create {label} parent directory {}",
            parent.display()
        )
    })?;

    let sequence = STAGING_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    let staging_name = format!(".toolframe-{label}-{}-{sequence}.tmp", std::process::id());
    let staging = parent.join(staging_name);
    fs::create_dir(&staging).with_context(|| {
        format!(
            "could not create temporary {label} scaffold {}",
            staging.display()
        )
    })?;

    let mut published = false;
    let result = (|| -> Result<()> {
        for (relative, content) in files {
            let destination = staging.join(relative);
            if let Some(parent) = destination.parent() {
                fs::create_dir_all(parent)
                    .with_context(|| format!("could not create {}", parent.display()))?;
            }
            write_scaffold_file(&destination, content)?;
        }

        // Publish the complete tree with no-replace semantics. A second launcher
        // may have created the user-owned directory while this tree was staged.
        published = rename_directory_if_absent(&staging, path)?;
        Ok(())
    })();

    if staging.exists() {
        fs::remove_dir_all(&staging).with_context(|| {
            format!(
                "could not remove temporary {label} scaffold {}",
                staging.display()
            )
        })?;
    }
    result?;

    // If another launcher won the race, its directory remains untouched.
    Ok(published)
}

#[cfg(target_vendor = "apple")]
fn rename_file_if_absent(source: &Path, destination: &Path) -> Result<bool> {
    use std::{ffi::CString, os::unix::ffi::OsStrExt};

    let source = CString::new(source.as_os_str().as_bytes())
        .context("temporary llms file path contains a NUL byte")?;
    let destination = CString::new(destination.as_os_str().as_bytes())
        .context("llms file path contains a NUL byte")?;
    // SAFETY: both pointers reference live NUL-terminated path buffers, and
    // RENAME_EXCL asks the kernel not to replace an existing destination.
    let result =
        unsafe { libc::renamex_np(source.as_ptr(), destination.as_ptr(), libc::RENAME_EXCL) };
    if result == 0 {
        return Ok(true);
    }
    let error = std::io::Error::last_os_error();
    if matches!(
        error.raw_os_error(),
        Some(libc::EEXIST) | Some(libc::ENOTEMPTY)
    ) {
        return Ok(false);
    }
    Err(error).with_context(|| {
        format!(
            "could not publish llms file at {}",
            destination.to_string_lossy()
        )
    })
}

#[cfg(any(target_os = "linux", target_os = "android"))]
fn rename_file_if_absent(source: &Path, destination: &Path) -> Result<bool> {
    use std::{ffi::CString, os::unix::ffi::OsStrExt};

    let source = CString::new(source.as_os_str().as_bytes())
        .context("temporary llms file path contains a NUL byte")?;
    let destination = CString::new(destination.as_os_str().as_bytes())
        .context("llms file path contains a NUL byte")?;
    // SAFETY: both pointers reference live NUL-terminated path buffers, and
    // RENAME_NOREPLACE asks the kernel not to replace an existing destination.
    let result = unsafe {
        libc::renameat2(
            libc::AT_FDCWD,
            source.as_ptr(),
            libc::AT_FDCWD,
            destination.as_ptr(),
            libc::RENAME_NOREPLACE,
        )
    };
    if result == 0 {
        return Ok(true);
    }
    let error = std::io::Error::last_os_error();
    if matches!(
        error.raw_os_error(),
        Some(libc::EEXIST) | Some(libc::ENOTEMPTY)
    ) {
        return Ok(false);
    }
    Err(error).with_context(|| {
        format!(
            "could not publish llms file at {}",
            destination.to_string_lossy()
        )
    })
}

#[cfg(not(any(target_vendor = "apple", target_os = "linux", target_os = "android")))]
fn rename_file_if_absent(source: &Path, destination: &Path) -> Result<bool> {
    match fs::hard_link(source, destination) {
        Ok(()) => {
            fs::remove_file(source).with_context(|| {
                format!(
                    "could not remove llms file staging path {}",
                    source.display()
                )
            })?;
            Ok(true)
        }
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => Ok(false),
        Err(error) => Err(error)
            .with_context(|| format!("could not publish llms file at {}", destination.display())),
    }
}

#[cfg(target_vendor = "apple")]
fn rename_directory_if_absent(source: &Path, destination: &Path) -> Result<bool> {
    use std::{ffi::CString, os::unix::ffi::OsStrExt};

    let source = CString::new(source.as_os_str().as_bytes())
        .context("temporary dashboard path contains a NUL byte")?;
    let destination = CString::new(destination.as_os_str().as_bytes())
        .context("dashboard path contains a NUL byte")?;
    // SAFETY: both pointers reference live NUL-terminated path buffers, and
    // RENAME_EXCL asks the kernel not to replace an existing destination.
    let result =
        unsafe { libc::renamex_np(source.as_ptr(), destination.as_ptr(), libc::RENAME_EXCL) };
    if result == 0 {
        return Ok(true);
    }
    let error = std::io::Error::last_os_error();
    if matches!(
        error.raw_os_error(),
        Some(libc::EEXIST) | Some(libc::ENOTEMPTY)
    ) {
        return Ok(false);
    }
    Err(error).with_context(|| {
        format!(
            "could not publish scaffold at {}",
            destination.to_string_lossy()
        )
    })
}

#[cfg(any(target_os = "linux", target_os = "android"))]
fn rename_directory_if_absent(source: &Path, destination: &Path) -> Result<bool> {
    use std::{ffi::CString, os::unix::ffi::OsStrExt};

    let source = CString::new(source.as_os_str().as_bytes())
        .context("temporary dashboard path contains a NUL byte")?;
    let destination = CString::new(destination.as_os_str().as_bytes())
        .context("dashboard path contains a NUL byte")?;
    // SAFETY: both pointers reference live NUL-terminated path buffers, and
    // RENAME_NOREPLACE asks the kernel not to replace an existing destination.
    let result = unsafe {
        libc::renameat2(
            libc::AT_FDCWD,
            source.as_ptr(),
            libc::AT_FDCWD,
            destination.as_ptr(),
            libc::RENAME_NOREPLACE,
        )
    };
    if result == 0 {
        return Ok(true);
    }
    let error = std::io::Error::last_os_error();
    if matches!(
        error.raw_os_error(),
        Some(libc::EEXIST) | Some(libc::ENOTEMPTY)
    ) {
        return Ok(false);
    }
    Err(error).with_context(|| {
        format!(
            "could not publish scaffold at {}",
            destination.to_string_lossy()
        )
    })
}

#[cfg(not(any(target_vendor = "apple", target_os = "linux", target_os = "android")))]
fn rename_directory_if_absent(source: &Path, destination: &Path) -> Result<bool> {
    if destination.exists() {
        return Ok(false);
    }
    match fs::rename(source, destination) {
        Ok(()) => Ok(true),
        Err(_) if destination.exists() => Ok(false),
        Err(error) => Err(error)
            .with_context(|| format!("could not publish scaffold at {}", destination.display())),
    }
}

fn write_scaffold_file(path: &Path, content: &str) -> Result<()> {
    let mut file = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(path)
        .with_context(|| format!("could not create scaffold file {}", path.display()))?;
    file.write_all(content.as_bytes())
        .with_context(|| format!("could not write {}", path.display()))?;
    file.sync_all()
        .with_context(|| format!("could not sync {}", path.display()))?;
    Ok(())
}

fn write_managed(path: &Path, content: &str) -> Result<()> {
    if fs::read(path).is_ok_and(|current| current == content.as_bytes()) {
        return Ok(());
    }

    let temporary = path.with_extension(format!("tmp-{}", std::process::id()));
    fs::write(&temporary, content)
        .with_context(|| format!("could not write temporary file {}", temporary.display()))?;
    fs::rename(&temporary, path)
        .with_context(|| format!("could not replace managed file {}", path.display()))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn installed_skill_files(root: &Path) -> BTreeSet<PathBuf> {
        fn visit(path: &Path, root: &Path, files: &mut BTreeSet<PathBuf>) {
            for entry in fs::read_dir(path).expect("read installed skill directory") {
                let entry = entry.expect("read installed skill entry");
                let path = entry.path();
                if path.is_dir() {
                    visit(&path, root, files);
                } else {
                    files.insert(path.strip_prefix(root).expect("relative skill path").into());
                }
            }
        }

        let mut files = BTreeSet::new();
        visit(root, root, &mut files);
        files
    }

    #[test]
    fn installs_exact_runtime_skill_files_without_evals_and_only_once() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let root = directory.path();
        let target = root.join(".agents/skills/toolframe-business-app");

        assert!(install_business_skill(root).expect("install business skill"));

        let expected_files: BTreeSet<PathBuf> = EMBEDDED_BUSINESS_SKILL_FILES
            .iter()
            .map(|(relative, _)| PathBuf::from(relative))
            .collect();
        let source_root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("skills")
            .join("toolframe-business-app");
        let source_production_files = installed_skill_files(&source_root)
            .into_iter()
            .filter(|relative| {
                !relative.starts_with("evals") && !relative.starts_with("evaluations")
            })
            .collect::<BTreeSet<_>>();
        assert_eq!(
            source_production_files, expected_files,
            "embedded production file list must follow the source skill"
        );
        assert!(EMBEDDED_BUSINESS_SKILL_FILES.iter().all(|(relative, _)| {
            !relative.starts_with("evals/") && !relative.starts_with("evaluations/")
        }));
        assert_eq!(installed_skill_files(&target), expected_files);
        for (relative, expected) in EMBEDDED_BUSINESS_SKILL_FILES {
            assert_eq!(
                fs::read(target.join(relative)).expect("read installed skill file"),
                *expected,
                "installed bytes differ for {relative}"
            );
        }
        assert!(!target.join("evals").exists());
        assert!(!target.join("evaluations").exists());

        let before = installed_skill_files(&target)
            .into_iter()
            .map(|relative| {
                let bytes = fs::read(target.join(&relative)).expect("read installed bytes");
                (relative, bytes)
            })
            .collect::<Vec<_>>();
        assert!(!install_business_skill(root).expect("skip existing business skill"));
        let after = installed_skill_files(&target)
            .into_iter()
            .map(|relative| {
                let bytes = fs::read(target.join(&relative)).expect("read preserved bytes");
                (relative, bytes)
            })
            .collect::<Vec<_>>();
        assert_eq!(after, before);
    }

    #[test]
    fn preserves_an_existing_real_skill_directory_without_repairing_it() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let root = directory.path();
        let target = root.join(".agents/skills/toolframe-business-app");
        fs::create_dir_all(&target).expect("create existing skill directory");
        fs::write(target.join("SKILL.md"), b"owner version\n").expect("write owner skill");
        fs::create_dir(target.join("custom")).expect("create owner directory");
        fs::write(target.join("custom/notes.md"), b"owner notes\n").expect("write owner notes");

        assert!(!install_business_skill(root).expect("preserve existing skill"));
        assert_eq!(
            fs::read(target.join("SKILL.md")).expect("read owner skill"),
            b"owner version\n"
        );
        assert_eq!(
            fs::read(target.join("custom/notes.md")).expect("read owner notes"),
            b"owner notes\n"
        );
        assert!(!target.join("agents").exists());
        assert!(!target.join("references").exists());
    }

    #[test]
    fn rejects_file_paths_in_the_skill_install_chain() {
        let cases = [
            (".agents", ".agents"),
            (".agents/skills", ".agents/skills"),
            (".agents/skills/toolframe-business-app", "target path"),
        ];
        for (relative, label) in cases {
            let directory = tempfile::tempdir().expect("create temp directory");
            let path = directory.path().join(relative);
            if let Some(parent) = path.parent() {
                fs::create_dir_all(parent).expect("create path parent");
            }
            fs::write(&path, b"occupied").expect("create invalid file path");

            let error = install_business_skill(directory.path())
                .expect_err("file path should be rejected")
                .to_string();
            assert!(error.contains(&path.display().to_string()), "{error}");
            assert!(error.contains("is a file"), "{error}");
            if label == ".agents" {
                assert!(error.contains(".agents path"), "{error}");
            } else if label == ".agents/skills" {
                assert!(error.contains(".agents/skills path"), "{error}");
            } else {
                assert!(error.contains("target path"), "{error}");
            }
        }
    }

    #[cfg(unix)]
    #[test]
    fn rejects_symlinks_in_the_skill_install_chain() {
        use std::os::unix::fs::symlink;

        let cases = [
            (".agents", ".agents"),
            (".agents/skills", ".agents/skills"),
            (".agents/skills/toolframe-business-app", "target path"),
        ];
        for (relative, label) in cases {
            let directory = tempfile::tempdir().expect("create temp directory");
            let path = directory.path().join(relative);
            if let Some(parent) = path.parent() {
                fs::create_dir_all(parent).expect("create symlink parent");
            }
            let destination = directory.path().join("real-destination");
            fs::create_dir_all(&destination).expect("create symlink destination");
            symlink(&destination, &path).expect("create invalid symlink path");

            let error = install_business_skill(directory.path())
                .expect_err("symlink path should be rejected")
                .to_string();
            assert!(error.contains(&path.display().to_string()), "{error}");
            assert!(error.contains("is a symlink"), "{error}");
            if label == ".agents" {
                assert!(error.contains(".agents path"), "{error}");
            } else if label == ".agents/skills" {
                assert!(error.contains(".agents/skills path"), "{error}");
            } else {
                assert!(error.contains("target path"), "{error}");
            }
        }
    }

    #[test]
    fn concurrent_skill_install_has_one_winner_and_no_partial_tree() {
        use std::sync::{Arc, Barrier};

        let directory = tempfile::tempdir().expect("create temp directory");
        let root = Arc::new(directory.path().to_path_buf());
        let barrier = Arc::new(Barrier::new(2));
        let handles = (0..2)
            .map(|_| {
                let root = Arc::clone(&root);
                let barrier = Arc::clone(&barrier);
                std::thread::spawn(move || {
                    barrier.wait();
                    install_business_skill(&root)
                })
            })
            .collect::<Vec<_>>();
        let results = handles
            .into_iter()
            .map(|handle| handle.join().expect("join installer"))
            .collect::<Result<Vec<_>>>()
            .expect("concurrent installers should succeed");
        assert_eq!(results.iter().filter(|installed| **installed).count(), 1);
        assert_eq!(results.iter().filter(|installed| !**installed).count(), 1);

        let target = root.join(".agents/skills/toolframe-business-app");
        let expected_files: BTreeSet<PathBuf> = EMBEDDED_BUSINESS_SKILL_FILES
            .iter()
            .map(|(relative, _)| PathBuf::from(relative))
            .collect();
        assert_eq!(installed_skill_files(&target), expected_files);
        for entry in fs::read_dir(root.join(".agents/skills")).expect("read skill parent") {
            let entry = entry.expect("read skill parent entry");
            let name = entry.file_name();
            let name = name.to_string_lossy();
            assert!(
                !name.starts_with(".toolframe-business-app-") || !name.ends_with(".tmp"),
                "partial staging directory remains: {name}"
            );
        }
    }

    #[test]
    fn materializes_tool_support_modules_in_the_managed_runtime() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let runtime = directory.path().join("runtime");
        let catalog = ToolCatalog {
            manifest: crate::tools::ToolManifest {
                version: 1,
                python_version: "3.14.0",
                tools_dir: Some(directory.path().join("tools")),
                domains_dir: None,
                tools: Vec::new(),
            },
            dependencies: Vec::new(),
        };
        let dashboard = DashboardManifest {
            requires_python: ">=3.14,<3.15".to_owned(),
            dependencies: Vec::new(),
        };

        let prepared = prepare_runtime(&runtime, &catalog, &dashboard).expect("prepare runtime");
        let core = prepared
            .core_server
            .parent()
            .expect("managed core directory");

        assert_eq!(
            fs::read_to_string(core.join("toolframe_db.py")).expect("read database support"),
            CORE_DATABASE
        );
        assert_eq!(
            fs::read_to_string(core.join("toolframe_idempotency.py"))
                .expect("read idempotency support"),
            CORE_IDEMPOTENCY
        );
        assert_eq!(
            fs::read_to_string(core.join("toolframe_artifacts.py")).expect("read artifact support"),
            CORE_ARTIFACTS
        );
    }

    #[test]
    fn scaffolds_default_tools_only_when_creating_a_new_tools_directory() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let paths = AppPaths {
            database: directory.path().join("data.db"),
            migrations: directory.path().join("nested").join("migrations"),
            tools: directory.path().join("nested").join("tools"),
            dashboard: directory.path().join("nested").join("dashboard"),
            workspace: directory.path().join("nested").join("workspace"),
            runtime: directory.path().join("nested").join("runtime"),
            llms_file: directory.path().join("nested").join("llms.txt"),
            llms_dir: directory.path().join("nested").join("llms"),
        };

        let prepared = prepare_directories(&paths).expect("prepare new directories");
        assert!(prepared.tools_scaffolded);
        assert!(prepared.dashboard_scaffolded);
        assert!(prepared.migrations_scaffolded);
        let sql = paths.tools.join("execute_sql.py");
        let artifact_example = paths.tools.join("_artifact_example.py");
        assert!(sql.is_file());
        assert_eq!(
            fs::read_to_string(&artifact_example).expect("read artifact example"),
            ARTIFACT_TOOL_EXAMPLE
        );
        fs::write(&sql, "# user-owned\n").expect("replace scaffold as user");
        fs::remove_file(&artifact_example).expect("remove private example");

        let prepared = prepare_directories(&paths).expect("prepare existing directories");
        assert!(!prepared.tools_scaffolded);
        assert!(!prepared.dashboard_scaffolded);
        assert!(!prepared.migrations_scaffolded);
        assert_eq!(
            fs::read_to_string(sql).expect("read user tool"),
            "# user-owned\n"
        );
        assert!(
            !artifact_example.exists(),
            "existing tools directory must not be repaired"
        );
    }

    #[test]
    fn scaffolds_llms_file_and_directory_once_without_repairing_user_content() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let llms_dir = directory.path().join("llms");
        let paths = AppPaths {
            database: directory.path().join("data").join("data.db"),
            migrations: directory.path().join("migrations"),
            tools: directory.path().join("tools"),
            dashboard: directory.path().join("dashboard"),
            workspace: directory.path().join("workspace"),
            runtime: directory.path().join(".toolframe"),
            llms_file: llms_dir.join("llms.txt"),
            llms_dir,
        };

        let prepared = prepare_directories(&paths).expect("prepare llms defaults");
        assert!(prepared.llms_file_scaffolded);
        assert!(prepared.llms_dir_scaffolded);
        assert!(paths.llms_file.is_file());
        assert_eq!(
            fs::read_to_string(&paths.llms_file).expect("read default llms file"),
            DEFAULT_LLMS_FILE
        );
        assert!(DEFAULT_LLMS_FILE.starts_with("# Unnamed\n"));
        assert!(DEFAULT_LLMS_FILE.contains("built on the Toolframe framework"));
        for (relative, expected) in EMBEDDED_LLMS_TREE {
            let child = paths.llms_dir.join(relative);
            assert!(child.is_file(), "missing llms scaffold file {relative}");
            assert_eq!(
                fs::read_to_string(child).expect("read llms scaffold file"),
                *expected
            );
        }

        fs::write(&paths.llms_file, "# user-owned guide\n").expect("replace llms guide");
        fs::remove_file(paths.llms_dir.join("workflow.md")).expect("remove llms workflow");
        let prepared = prepare_directories(&paths).expect("prepare existing llms paths");
        assert!(!prepared.llms_file_scaffolded);
        assert!(!prepared.llms_dir_scaffolded);
        assert_eq!(
            fs::read_to_string(&paths.llms_file).expect("read user llms guide"),
            "# user-owned guide\n"
        );
        assert!(
            !paths.llms_dir.join("workflow.md").exists(),
            "existing llms directory must not be repaired"
        );
    }

    #[test]
    fn existing_llms_directory_with_an_index_is_not_repaired() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let llms_dir = directory.path().join("llms");
        let paths = AppPaths {
            database: directory.path().join("data").join("data.db"),
            migrations: directory.path().join("migrations"),
            tools: directory.path().join("tools"),
            dashboard: directory.path().join("dashboard"),
            workspace: directory.path().join("workspace"),
            runtime: directory.path().join(".toolframe"),
            llms_file: llms_dir.join("llms.txt"),
            llms_dir,
        };
        fs::create_dir(&paths.llms_dir).expect("create user llms directory");
        fs::write(&paths.llms_file, "# user-owned guide\n").expect("create user llms guide");

        let prepared = prepare_directories(&paths).expect("prepare existing llms directory");
        assert!(!prepared.llms_file_scaffolded);
        assert!(!prepared.llms_dir_scaffolded);
        assert_eq!(
            fs::read_dir(&paths.llms_dir)
                .expect("read llms directory")
                .count(),
            1
        );
    }

    #[test]
    fn rejects_a_partial_llms_source_without_repairing_the_missing_peer() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let root_only = AppPaths {
            database: directory.path().join("root-only-data.db"),
            migrations: directory.path().join("root-only-migrations"),
            tools: directory.path().join("root-only-tools"),
            dashboard: directory.path().join("root-only-dashboard"),
            workspace: directory.path().join("root-only-workspace"),
            runtime: directory.path().join("root-only-runtime"),
            llms_file: directory.path().join("root-only-llms.txt"),
            llms_dir: directory.path().join("root-only-llms"),
        };
        fs::write(&root_only.llms_file, "# user-owned guide\n")
            .expect("create root-only llms file");
        let error = prepare_directories(&root_only)
            .expect_err("root-only llms source must be rejected")
            .to_string();
        assert!(error.contains(&root_only.llms_dir.display().to_string()));
        assert!(error.contains("both must be present or both absent"));
        assert!(!root_only.llms_dir.exists());

        let dir_only = AppPaths {
            database: directory.path().join("dir-only-data.db"),
            migrations: directory.path().join("dir-only-migrations"),
            tools: directory.path().join("dir-only-tools"),
            dashboard: directory.path().join("dir-only-dashboard"),
            workspace: directory.path().join("dir-only-workspace"),
            runtime: directory.path().join("dir-only-runtime"),
            llms_file: directory.path().join("dir-only-llms.txt"),
            llms_dir: directory.path().join("dir-only-llms"),
        };
        fs::create_dir(&dir_only.llms_dir).expect("create dir-only llms directory");
        let error = prepare_directories(&dir_only)
            .expect_err("dir-only llms source must be rejected")
            .to_string();
        assert!(error.contains(&dir_only.llms_file.display().to_string()));
        assert!(error.contains("both must be present or both absent"));
        assert!(!dir_only.llms_file.exists());
    }

    #[cfg(unix)]
    #[test]
    fn rejects_llms_wrong_types_and_symlinks_with_path_and_reason() {
        use std::os::unix::fs::symlink;

        let directory = tempfile::tempdir().expect("create temp directory");
        let wrong_file = AppPaths {
            database: directory.path().join("wrong-file-data.db"),
            migrations: directory.path().join("wrong-file-migrations"),
            tools: directory.path().join("wrong-file-tools"),
            dashboard: directory.path().join("wrong-file-dashboard"),
            workspace: directory.path().join("wrong-file-workspace"),
            runtime: directory.path().join("wrong-file-runtime"),
            llms_file: directory.path().join("wrong-file-llms.txt"),
            llms_dir: directory.path().join("wrong-file-llms"),
        };
        fs::create_dir(&wrong_file.llms_file).expect("create wrong llms file directory");
        let error = prepare_directories(&wrong_file)
            .expect_err("llms file directory must be rejected")
            .to_string();
        assert!(error.contains(&wrong_file.llms_file.display().to_string()));
        assert!(error.contains("expected a regular file"));

        let wrong_dir = AppPaths {
            database: directory.path().join("wrong-dir-data.db"),
            migrations: directory.path().join("wrong-dir-migrations"),
            tools: directory.path().join("wrong-dir-tools"),
            dashboard: directory.path().join("wrong-dir-dashboard"),
            workspace: directory.path().join("wrong-dir-workspace"),
            runtime: directory.path().join("wrong-dir-runtime"),
            llms_file: directory.path().join("wrong-dir-llms.txt"),
            llms_dir: directory.path().join("wrong-dir-llms"),
        };
        fs::write(&wrong_dir.llms_file, "# guide\n").expect("create valid llms file");
        fs::write(&wrong_dir.llms_dir, "not a directory").expect("create wrong llms dir file");
        let error = prepare_directories(&wrong_dir)
            .expect_err("llms directory file must be rejected")
            .to_string();
        assert!(error.contains(&wrong_dir.llms_dir.display().to_string()));
        assert!(error.contains("expected a real directory"));

        let symlink_file = AppPaths {
            database: directory.path().join("symlink-file-data.db"),
            migrations: directory.path().join("symlink-file-migrations"),
            tools: directory.path().join("symlink-file-tools"),
            dashboard: directory.path().join("symlink-file-dashboard"),
            workspace: directory.path().join("symlink-file-workspace"),
            runtime: directory.path().join("symlink-file-runtime"),
            llms_file: directory.path().join("symlink-file-llms.txt"),
            llms_dir: directory.path().join("symlink-file-llms"),
        };
        fs::write(directory.path().join("guide-target.txt"), "# target\n")
            .expect("create llms file symlink target");
        fs::create_dir(&symlink_file.llms_dir).expect("create valid llms directory");
        symlink(
            directory.path().join("guide-target.txt"),
            &symlink_file.llms_file,
        )
        .expect("create llms file symlink");
        let error = prepare_directories(&symlink_file)
            .expect_err("llms file symlink must be rejected")
            .to_string();
        assert!(error.contains(&symlink_file.llms_file.display().to_string()));
        assert!(error.contains("symlink"));

        let symlink_dir = AppPaths {
            database: directory.path().join("symlink-dir-data.db"),
            migrations: directory.path().join("symlink-dir-migrations"),
            tools: directory.path().join("symlink-dir-tools"),
            dashboard: directory.path().join("symlink-dir-dashboard"),
            workspace: directory.path().join("symlink-dir-workspace"),
            runtime: directory.path().join("symlink-dir-runtime"),
            llms_file: directory.path().join("symlink-dir-llms.txt"),
            llms_dir: directory.path().join("symlink-dir-llms"),
        };
        fs::write(&symlink_dir.llms_file, "# guide\n").expect("create valid llms file");
        let target = directory.path().join("real-llms-directory");
        fs::create_dir(&target).expect("create llms directory symlink target");
        symlink(&target, &symlink_dir.llms_dir).expect("create llms directory symlink");
        let error = prepare_directories(&symlink_dir)
            .expect_err("llms directory symlink must be rejected")
            .to_string();
        assert!(error.contains(&symlink_dir.llms_dir.display().to_string()));
        assert!(error.contains("symlink"));
    }

    #[test]
    fn resolves_defaults_beside_the_executable_root() {
        let root = Path::new("/tmp/toolframe-dist");
        let paths = resolve_paths(root, None, None, None, None, None, None, None, None)
            .expect("resolve paths");
        assert_eq!(paths.database, root.join("data").join("data.db"));
        assert_eq!(paths.migrations, root.join("migrations"));
        assert_eq!(paths.tools, root.join("tools"));
        assert_eq!(paths.dashboard, root.join("dashboard"));
        assert_eq!(paths.workspace, root.join("workspace"));
        assert_eq!(paths.runtime, root.join(".toolframe"));
        assert_eq!(paths.llms_file, root.join("llms").join("llms.txt"));
        assert_eq!(paths.llms_dir, root.join("llms"));
    }

    #[test]
    fn resolves_and_prepares_logs_relative_to_executable_root() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let logs = prepare_logs_dir(directory.path(), Some(Path::new("custom-logs")))
            .expect("prepare relative logs");
        assert_eq!(
            logs,
            directory
                .path()
                .join("custom-logs")
                .canonicalize()
                .expect("canonical logs")
        );

        let defaults = prepare_logs_dir(directory.path(), None).expect("prepare default logs");
        assert_eq!(
            defaults,
            directory
                .path()
                .join(".toolframe")
                .join("logs")
                .canonicalize()
                .expect("canonical default logs")
        );
    }

    #[test]
    fn rejects_a_logs_path_that_is_a_file() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let path = directory.path().join("not-a-directory");
        fs::write(&path, "occupied").expect("create file");
        assert!(prepare_logs_dir(directory.path(), Some(&path)).is_err());
    }

    #[test]
    fn resolves_relative_dashboard_override_from_the_current_directory() {
        let root = Path::new("/tmp/toolframe-dist");
        let cwd = env::current_dir().expect("read current directory");
        let paths = resolve_paths(
            root,
            None,
            None,
            None,
            Some(PathBuf::from("custom-dashboard")),
            None,
            None,
            None,
            None,
        )
        .expect("resolve paths");
        assert_eq!(paths.dashboard, cwd.join("custom-dashboard"));
    }

    #[test]
    fn resolves_relative_migrations_override_from_the_current_directory() {
        let root = Path::new("/tmp/toolframe-dist");
        let cwd = env::current_dir().expect("read current directory");
        let paths = resolve_paths(
            root,
            None,
            Some(PathBuf::from("custom-migrations")),
            None,
            None,
            None,
            None,
            None,
            None,
        )
        .expect("resolve paths");
        assert_eq!(paths.migrations, cwd.join("custom-migrations"));
    }

    #[test]
    fn migration_scaffold_is_complete_and_never_repairs_an_existing_directory() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let paths = AppPaths {
            database: directory.path().join("data.db"),
            migrations: directory.path().join("migrations"),
            tools: directory.path().join("tools"),
            dashboard: directory.path().join("dashboard"),
            workspace: directory.path().join("workspace"),
            runtime: directory.path().join("runtime"),
            llms_file: directory.path().join("llms.txt"),
            llms_dir: directory.path().join("llms"),
        };

        let prepared = prepare_directories(&paths).expect("prepare new directories");
        assert!(prepared.migrations_scaffolded);
        let bootstrap = paths.migrations.join("0000_bootstrap.sql");
        assert_eq!(
            fs::read_to_string(&bootstrap).expect("read bootstrap"),
            DEFAULT_BOOTSTRAP_MIGRATION
        );
        assert_eq!(
            fs::read_to_string(paths.migrations.join("README.md"))
                .expect("read migration reference"),
            DEFAULT_MIGRATION_README
        );

        fs::write(&bootstrap, "-- user-owned migration\n").expect("replace bootstrap");
        let prepared = prepare_directories(&paths).expect("prepare existing directories");
        assert!(!prepared.migrations_scaffolded);
        assert_eq!(
            fs::read_to_string(bootstrap).expect("read user migration"),
            "-- user-owned migration\n"
        );
    }

    #[test]
    fn existing_empty_migrations_directory_is_not_scaffolded() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let paths = AppPaths {
            database: directory.path().join("data.db"),
            migrations: directory.path().join("migrations"),
            tools: directory.path().join("tools"),
            dashboard: directory.path().join("dashboard"),
            workspace: directory.path().join("workspace"),
            runtime: directory.path().join("runtime"),
            llms_file: directory.path().join("llms.txt"),
            llms_dir: directory.path().join("llms"),
        };
        fs::create_dir(&paths.migrations).expect("create user migrations directory");

        let prepared = prepare_directories(&paths).expect("prepare directories");
        assert!(!prepared.migrations_scaffolded);
        assert!(
            fs::read_dir(&paths.migrations)
                .expect("read migrations")
                .next()
                .is_none()
        );
    }

    #[test]
    fn dashboard_scaffold_is_complete_and_never_repairs_an_existing_directory() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let paths = AppPaths {
            database: directory.path().join("data.db"),
            migrations: directory.path().join("migrations"),
            tools: directory.path().join("tools"),
            dashboard: directory.path().join("dashboard"),
            workspace: directory.path().join("workspace"),
            runtime: directory.path().join("runtime"),
            llms_file: directory.path().join("llms.txt"),
            llms_dir: directory.path().join("llms"),
        };

        let prepared = prepare_directories(&paths).expect("prepare new directories");
        assert!(prepared.dashboard_scaffolded);
        for relative in [
            "app.py",
            "pyproject.toml",
            "components/__init__.py",
            "widgets/__init__.py",
            "widgets/example_card.py",
        ] {
            assert!(
                paths.dashboard.join(relative).is_file(),
                "missing scaffold file {relative}"
            );
        }

        let app = paths.dashboard.join("app.py");
        let widgets_init = paths.dashboard.join("widgets/__init__.py");
        fs::write(&app, "# user-owned dashboard\n").expect("replace dashboard app");
        fs::remove_file(&widgets_init).expect("remove a scaffold file");

        let prepared = prepare_directories(&paths).expect("prepare existing directories");
        assert!(!prepared.dashboard_scaffolded);
        assert_eq!(
            fs::read_to_string(app).expect("read dashboard app"),
            "# user-owned dashboard\n"
        );
        assert!(
            !widgets_init.exists(),
            "existing dashboard must not be repaired"
        );
    }

    #[test]
    fn existing_empty_dashboard_directory_is_not_scaffolded() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let paths = AppPaths {
            database: directory.path().join("data.db"),
            migrations: directory.path().join("migrations"),
            tools: directory.path().join("tools"),
            dashboard: directory.path().join("dashboard"),
            workspace: directory.path().join("workspace"),
            runtime: directory.path().join("runtime"),
            llms_file: directory.path().join("llms.txt"),
            llms_dir: directory.path().join("llms"),
        };
        fs::create_dir(&paths.dashboard).expect("create user dashboard directory");

        let prepared = prepare_directories(&paths).expect("prepare directories");
        assert!(!prepared.dashboard_scaffolded);
        assert!(
            fs::read_dir(&paths.dashboard)
                .expect("read dashboard")
                .next()
                .is_none()
        );
    }

    #[test]
    fn host_and_origin_lists_include_loopback() {
        let hosts = allowed_hosts(IpAddr::V4(Ipv4Addr::UNSPECIFIED));
        let origins = allowed_origins(IpAddr::V4(Ipv4Addr::UNSPECIFIED), 8123);
        assert!(hosts.iter().any(|host| host == "localhost"));
        assert!(hosts.iter().any(|host| host == "127.0.0.1"));
        assert!(
            origins
                .iter()
                .any(|origin| origin == "http://localhost:8123")
        );
        assert!(!origins.iter().any(|origin| origin.contains("example.com")));
    }
}
