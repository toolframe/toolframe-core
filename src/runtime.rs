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
    AppManifest, ToolCatalog, catalog_json, content_hash, runtime_dependencies,
    runtime_project_toml,
};

const CORE_SERVER: &str = include_str!("../core/server.py");
const CORE_SUPPORT: &str = include_str!("../core/toolframe_runtime.py");
const CORE_ARCHITECTURE: &str = include_str!("../core/toolframe_architecture.py");
const CORE_DASHBOARD: &str = include_str!("../core/toolframe_dashboard.py");
const CORE_DATABASE: &str = include_str!("../core/toolframe_db.py");
const CORE_IDEMPOTENCY: &str = include_str!("../core/toolframe_idempotency.py");
const CORE_ARTIFACTS: &str = include_str!("../core/toolframe_artifacts.py");
const CORE_DASHBOARD_RUNNER: &str = include_str!("../core/toolframe_dashboard_runner.py");
const CORE_LOGGING: &str = include_str!("../core/toolframe_logging.py");
const CORE_CLI: &str = include_str!("../core/toolframe_cli.py");
const INFO_PAGE: &str = include_str!("../static/index.html");
const DEFAULT_APP_CONTEXT: &str = include_str!("../APP_CONTEXT.md");
const DEFAULT_DOMAIN_MODEL: &str = include_str!("../DOMAIN_MODEL.md");
const EMBEDDED_APP_TREE: &[(&str, &str)] = &[
    ("dashboard.py", include_str!("../app/dashboard.py")),
    ("llms.txt", include_str!("../app/llms.txt")),
    ("pyproject.toml", include_str!("../app/pyproject.toml")),
];
const EMBEDDED_DOMAINS_TREE: &[(&str, &str)] = &[
    (
        "entry_register/__init__.py",
        include_str!("../domains/entry_register/__init__.py"),
    ),
    (
        "entry_register/llms/capabilities.md",
        include_str!("../domains/entry_register/llms/capabilities.md"),
    ),
    (
        "entry_register/llms/workflow.md",
        include_str!("../domains/entry_register/llms/workflow.md"),
    ),
    (
        "entry_register/model/__init__.py",
        include_str!("../domains/entry_register/model/__init__.py"),
    ),
    (
        "entry_register/model/entries.py",
        include_str!("../domains/entry_register/model/entries.py"),
    ),
    (
        "entry_register/tools/__init__.py",
        include_str!("../domains/entry_register/tools/__init__.py"),
    ),
    (
        "entry_register/tools/_execute_sql_reference.py",
        include_str!("../domains/entry_register/tools/_execute_sql_reference.py"),
    ),
    (
        "entry_register/tools/entry_register_list_entries.py",
        include_str!("../domains/entry_register/tools/entry_register_list_entries.py"),
    ),
    (
        "entry_register/ui/__init__.py",
        include_str!("../domains/entry_register/ui/__init__.py"),
    ),
    (
        "entry_register/ui/entries.py",
        include_str!("../domains/entry_register/ui/entries.py"),
    ),
    (
        "entry_register/migrations/0000_bootstrap.sql",
        include_str!("../domains/entry_register/migrations/0000_bootstrap.sql"),
    ),
    (
        "entry_register/migrations/0001_adopt_domain_table_name.sql",
        include_str!("../domains/entry_register/migrations/0001_adopt_domain_table_name.sql"),
    ),
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
        "assets/ARTIFACT_TOOL.template.py",
        include_bytes!("../skills/toolframe-business-app/assets/ARTIFACT_TOOL.template.py"),
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
    pub root: PathBuf,
    pub database: PathBuf,
    pub app: PathBuf,
    pub domains: PathBuf,
    pub workspace: PathBuf,
    pub runtime: PathBuf,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PreparedDirectories {
    pub app_scaffolded: bool,
    pub domains_scaffolded: bool,
    pub app_context_scaffolded: bool,
    pub domain_model_scaffolded: bool,
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

pub fn resolve_paths(
    root: &Path,
    database: Option<PathBuf>,
    app: Option<PathBuf>,
    domains: Option<PathBuf>,
    workspace: Option<PathBuf>,
    runtime: Option<PathBuf>,
) -> Result<AppPaths> {
    let cwd = env::current_dir().context("could not read the current directory")?;
    Ok(AppPaths {
        root: root.to_path_buf(),
        database: absolute_override(database, &cwd, &root.join("data").join("data.db")),
        app: absolute_override(app, &cwd, &root.join("app")),
        domains: absolute_override(domains, &cwd, &root.join("domains")),
        workspace: absolute_override(workspace, &cwd, &root.join("workspace")),
        runtime: absolute_override(runtime, &cwd, &root.join(".toolframe")),
    })
}

pub fn prepare_directories(paths: &AppPaths) -> Result<PreparedDirectories> {
    prepare_database_parent(&paths.database)?;
    let app_scaffolded = scaffold_directory_if_missing(&paths.app, "app", EMBEDDED_APP_TREE)?;
    let domains_scaffolded =
        scaffold_directory_if_missing(&paths.domains, "domains", EMBEDDED_DOMAINS_TREE)?;
    let app_context_scaffolded = scaffold_file_if_missing(
        &paths.root.join("APP_CONTEXT.md"),
        "app-context",
        DEFAULT_APP_CONTEXT,
    )?;
    let domain_model_scaffolded = scaffold_file_if_missing(
        &paths.root.join("DOMAIN_MODEL.md"),
        "domain-model",
        DEFAULT_DOMAIN_MODEL,
    )?;
    create_directory_if_missing(&paths.workspace, "workspace")?;
    create_directory_if_missing(&paths.runtime, "runtime")?;

    Ok(PreparedDirectories {
        app_scaffolded,
        domains_scaffolded,
        app_context_scaffolded,
        domain_model_scaffolded,
    })
}

pub fn prepare_runtime(
    runtime_dir: &Path,
    catalog: &ToolCatalog,
    app: &AppManifest,
) -> Result<PreparedRuntime> {
    let core_hash = content_hash(
        [
            CORE_SERVER.as_bytes(),
            CORE_SUPPORT.as_bytes(),
            CORE_ARCHITECTURE.as_bytes(),
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
    let core_architecture = core_dir.join("toolframe_architecture.py");
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
    write_managed(&core_architecture, CORE_ARCHITECTURE)?;
    write_managed(&dashboard_sdk, CORE_DASHBOARD)?;
    write_managed(&database_sdk, CORE_DATABASE)?;
    write_managed(&idempotency, CORE_IDEMPOTENCY)?;
    write_managed(&artifacts_sdk, CORE_ARTIFACTS)?;
    write_managed(&dashboard_runner, CORE_DASHBOARD_RUNNER)?;
    write_managed(&core_logging, CORE_LOGGING)?;
    write_managed(&cli_runner, CORE_CLI)?;
    write_managed(&info_page, INFO_PAGE)?;

    let project_toml = runtime_project_toml(catalog, app)?;
    let dependencies = runtime_dependencies(catalog, app);
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

fn scaffold_file_if_missing(path: &Path, label: &str, content: &str) -> Result<bool> {
    match inspect_skill_path(path)? {
        SkillPathState::File => return Ok(false),
        SkillPathState::Missing => {}
        SkillPathState::Directory => bail!(
            "{label} path {} is a directory; expected a regular file",
            path.display()
        ),
        SkillPathState::Other => bail!("{label} path {} is not a regular file", path.display()),
        SkillPathState::Symlink => bail!(
            "{label} path {} is a symlink; refusing to follow it",
            path.display()
        ),
    }

    let parent = path
        .parent()
        .ok_or_else(|| anyhow!("{label} path {} has no parent directory", path.display()))?;
    fs::create_dir_all(parent)
        .with_context(|| format!("could not create {label} parent {}", parent.display()))?;
    let sequence = STAGING_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    let staging = parent.join(format!(
        ".toolframe-{label}-{}-{sequence}.tmp",
        std::process::id()
    ));
    write_scaffold_file(&staging, content)?;
    let published = rename_file_if_absent(&staging, path)?;
    if staging.exists() {
        fs::remove_file(&staging)
            .with_context(|| format!("could not remove staging file {}", staging.display()))?;
    }
    if !published {
        match inspect_skill_path(path)? {
            SkillPathState::File => {}
            SkillPathState::Symlink => bail!(
                "{label} path {} became a symlink; refusing to follow it",
                path.display()
            ),
            _ => bail!(
                "{label} path {} was not published as a regular file",
                path.display()
            ),
        }
    }
    Ok(published)
}

pub(crate) fn scaffold_directory_if_missing(
    path: &Path,
    label: &str,
    files: &[(&str, &str)],
) -> Result<bool> {
    match inspect_skill_path(path)? {
        SkillPathState::Directory => return Ok(false),
        SkillPathState::Missing => {}
        SkillPathState::File | SkillPathState::Other => bail!(
            "{label} path {} exists but is not a directory",
            path.display()
        ),
        SkillPathState::Symlink => bail!(
            "{label} path {} is a symlink; refusing to follow it",
            path.display()
        ),
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

    fn example_paths(root: &Path) -> AppPaths {
        AppPaths {
            root: root.to_path_buf(),
            database: root.join("data/data.db"),
            app: root.join("app"),
            domains: root.join("domains"),
            workspace: root.join("workspace"),
            runtime: root.join(".toolframe"),
        }
    }

    #[test]
    fn materializes_tool_support_modules_in_the_managed_runtime() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let runtime = directory.path().join("runtime");
        let catalog = ToolCatalog {
            manifest: crate::tools::ToolManifest {
                version: 2,
                python_version: "3.14.0",
                domains_dir: directory.path().join("domains"),
                tools: Vec::new(),
            },
            dependencies: Vec::new(),
        };
        let app = AppManifest {
            requires_python: ">=3.14,<3.15".to_owned(),
            dependencies: Vec::new(),
        };

        let prepared = prepare_runtime(&runtime, &catalog, &app).expect("prepare runtime");
        let core = prepared
            .core_server
            .parent()
            .expect("managed core directory");
        assert_eq!(
            fs::read_to_string(core.join("toolframe_db.py")).expect("read database support"),
            CORE_DATABASE
        );
        assert_eq!(
            fs::read_to_string(core.join("toolframe_architecture.py"))
                .expect("read architecture support"),
            CORE_ARCHITECTURE
        );
        assert_eq!(
            fs::read_to_string(core.join("toolframe_artifacts.py")).expect("read artifact support"),
            CORE_ARTIFACTS
        );
    }

    #[test]
    fn scaffolds_the_complete_new_source_contract_once() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let paths = example_paths(directory.path());

        let first = prepare_directories(&paths).expect("prepare fresh app");
        assert_eq!(
            first,
            PreparedDirectories {
                app_scaffolded: true,
                domains_scaffolded: true,
                app_context_scaffolded: true,
                domain_model_scaffolded: true,
            }
        );
        for (relative, expected) in EMBEDDED_APP_TREE {
            assert_eq!(
                fs::read_to_string(paths.app.join(relative)).expect("read app scaffold"),
                *expected
            );
        }
        for (relative, expected) in EMBEDDED_DOMAINS_TREE {
            assert_eq!(
                fs::read_to_string(paths.domains.join(relative)).expect("read domain scaffold"),
                *expected
            );
        }
        assert_eq!(
            fs::read_to_string(paths.root.join("APP_CONTEXT.md")).expect("read app context"),
            DEFAULT_APP_CONTEXT
        );
        assert_eq!(
            fs::read_to_string(paths.root.join("DOMAIN_MODEL.md")).expect("read domain model"),
            DEFAULT_DOMAIN_MODEL
        );

        fs::write(paths.app.join("dashboard.py"), "# user app\n").expect("edit app");
        fs::remove_file(paths.domains.join("entry_register/ui/entries.py")).expect("remove widget");
        fs::write(paths.root.join("APP_CONTEXT.md"), "# user context\n").expect("edit context");
        let second = prepare_directories(&paths).expect("preserve existing sources");
        assert_eq!(
            second,
            PreparedDirectories {
                app_scaffolded: false,
                domains_scaffolded: false,
                app_context_scaffolded: false,
                domain_model_scaffolded: false,
            }
        );
        assert_eq!(
            fs::read_to_string(paths.app.join("dashboard.py")).expect("read app"),
            "# user app\n"
        );
        assert!(!paths.domains.join("entry_register/ui/entries.py").exists());
        assert_eq!(
            fs::read_to_string(paths.root.join("APP_CONTEXT.md")).expect("read context"),
            "# user context\n"
        );
    }

    #[test]
    fn resolves_default_and_relative_app_and_domain_paths() {
        let root = Path::new("/tmp/toolframe-dist");
        let defaults = resolve_paths(root, None, None, None, None, None).expect("resolve defaults");
        assert_eq!(defaults.root, root);
        assert_eq!(defaults.database, root.join("data/data.db"));
        assert_eq!(defaults.app, root.join("app"));
        assert_eq!(defaults.domains, root.join("domains"));
        assert_eq!(defaults.workspace, root.join("workspace"));
        assert_eq!(defaults.runtime, root.join(".toolframe"));

        let cwd = env::current_dir().expect("read current directory");
        let custom = resolve_paths(
            root,
            None,
            Some(PathBuf::from("custom-app")),
            Some(PathBuf::from("custom-domains")),
            None,
            None,
        )
        .expect("resolve custom paths");
        assert_eq!(custom.app, cwd.join("custom-app"));
        assert_eq!(custom.domains, cwd.join("custom-domains"));
    }

    #[cfg(unix)]
    #[test]
    fn rejects_symlinked_source_contract_paths() {
        use std::os::unix::fs::symlink;

        let directory = tempfile::tempdir().expect("create temp directory");
        let paths = example_paths(directory.path());
        let real_app = directory.path().join("real-app");
        fs::create_dir(&real_app).expect("create real app");
        symlink(&real_app, &paths.app).expect("link app");
        let error = prepare_directories(&paths)
            .expect_err("linked app must fail")
            .to_string();
        assert!(error.contains("app path"));
        assert!(error.contains("symlink"));
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
