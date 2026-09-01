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
const CORE_SUPPORT: &str = include_str!("../core/lite_mcp_runtime.py");
const CORE_DASHBOARD: &str = include_str!("../core/lite_mcp_dashboard.py");
const CORE_DASHBOARD_RUNNER: &str = include_str!("../core/lite_mcp_dashboard_runner.py");
const INFO_PAGE: &str = include_str!("../static/index.html");
const DEFAULT_SQL_TOOL: &str = include_str!("../tools/execute_sql.py");
const DASHBOARD_APP: &str = include_str!("../dashboard/app.py");
const DASHBOARD_PROJECT: &str = include_str!("../dashboard/pyproject.toml");
const DASHBOARD_COMPONENTS_INIT: &str = include_str!("../dashboard/components/__init__.py");
const DASHBOARD_WIDGETS_INIT: &str = include_str!("../dashboard/widgets/__init__.py");
const DASHBOARD_EXAMPLE_CARD: &str = include_str!("../dashboard/widgets/example_card.py");
static STAGING_SEQUENCE: AtomicU64 = AtomicU64::new(0);

#[derive(Debug, Clone)]
pub struct AppPaths {
    pub database: PathBuf,
    pub tools: PathBuf,
    pub dashboard: PathBuf,
    pub workspace: PathBuf,
    pub runtime: PathBuf,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PreparedDirectories {
    pub tools_scaffolded: bool,
    pub dashboard_scaffolded: bool,
}

#[derive(Debug, Clone)]
pub struct PreparedRuntime {
    pub core_server: PathBuf,
    pub dashboard_sdk: PathBuf,
    pub dashboard_runner: PathBuf,
    pub info_page: PathBuf,
    pub manifest: PathBuf,
    pub project: PathBuf,
    pub dependencies: Vec<String>,
}

pub fn executable_root() -> Result<PathBuf> {
    let executable = env::current_exe().context("could not locate the lite-mcp executable")?;
    executable
        .parent()
        .map(Path::to_path_buf)
        .ok_or_else(|| anyhow!("the lite-mcp executable has no parent directory"))
}

pub fn resolve_paths(
    root: &Path,
    database: Option<PathBuf>,
    tools: Option<PathBuf>,
    dashboard: Option<PathBuf>,
    workspace: Option<PathBuf>,
    runtime: Option<PathBuf>,
) -> Result<AppPaths> {
    let cwd = env::current_dir().context("could not read the current directory")?;
    Ok(AppPaths {
        database: absolute_override(database, &cwd, &root.join("data.db")),
        tools: absolute_override(tools, &cwd, &root.join("tools")),
        dashboard: absolute_override(dashboard, &cwd, &root.join("dashboard")),
        workspace: absolute_override(workspace, &cwd, &root.join("workspace")),
        runtime: absolute_override(runtime, &cwd, &root.join("runtime")),
    })
}

pub fn prepare_directories(paths: &AppPaths) -> Result<PreparedDirectories> {
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
    }

    let dashboard_scaffolded = scaffold_dashboard_if_missing(&paths.dashboard)?;

    Ok(PreparedDirectories {
        tools_scaffolded: tools_created,
        dashboard_scaffolded,
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
            CORE_DASHBOARD_RUNNER.as_bytes(),
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
    let core_support = core_dir.join("lite_mcp_runtime.py");
    let dashboard_sdk = core_dir.join("lite_mcp_dashboard.py");
    let dashboard_runner = core_dir.join("lite_mcp_dashboard_runner.py");
    let info_page = core_dir.join("info.html");
    write_managed(&core_server, CORE_SERVER)?;
    write_managed(&core_support, CORE_SUPPORT)?;
    write_managed(&dashboard_sdk, CORE_DASHBOARD)?;
    write_managed(&dashboard_runner, CORE_DASHBOARD_RUNNER)?;
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
        dashboard_sdk,
        dashboard_runner,
        info_page,
        manifest,
        project,
        dependencies,
    })
}

pub fn locate_uv(explicit: Option<&Path>, executable_root: &Path) -> Result<PathBuf> {
    if let Some(path) = explicit {
        return validate_uv(path).context("the configured UV executable is invalid");
    }

    let sibling_name = if cfg!(windows) { "uv.exe" } else { "uv" };
    let sibling = executable_root.join(sibling_name);
    if sibling.is_file() {
        return validate_uv(&sibling).context("the bundled UV executable is invalid");
    }

    if let Some(path) = find_on_path(sibling_name) {
        return validate_uv(&path).context("the UV executable on PATH is invalid");
    }

    bail!(
        "could not find UV; pass --uv-path, set LITE_MCP_UV, place {sibling_name} beside lite-mcp, or install uv on PATH"
    )
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

fn scaffold_dashboard_if_missing(path: &Path) -> Result<bool> {
    if path.exists() {
        if path.is_dir() {
            return Ok(false);
        }
        bail!(
            "dashboard path {} exists but is not a directory",
            path.display()
        );
    }

    let parent = path
        .parent()
        .ok_or_else(|| anyhow!("dashboard path {} has no parent directory", path.display()))?;
    fs::create_dir_all(parent).with_context(|| {
        format!(
            "could not create dashboard parent directory {}",
            parent.display()
        )
    })?;

    let sequence = STAGING_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    let staging_name = format!(".lite-mcp-dashboard-{}-{sequence}.tmp", std::process::id());
    let staging = parent.join(staging_name);
    fs::create_dir(&staging).with_context(|| {
        format!(
            "could not create temporary dashboard scaffold {}",
            staging.display()
        )
    })?;

    let mut published = false;
    let result = (|| -> Result<()> {
        let components = staging.join("components");
        let widgets = staging.join("widgets");
        fs::create_dir(&components)
            .with_context(|| format!("could not create {}", components.display()))?;
        fs::create_dir(&widgets)
            .with_context(|| format!("could not create {}", widgets.display()))?;

        for (destination, content) in [
            (staging.join("app.py"), DASHBOARD_APP),
            (staging.join("pyproject.toml"), DASHBOARD_PROJECT),
            (components.join("__init__.py"), DASHBOARD_COMPONENTS_INIT),
            (widgets.join("__init__.py"), DASHBOARD_WIDGETS_INIT),
            (widgets.join("example_card.py"), DASHBOARD_EXAMPLE_CARD),
        ] {
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
                "could not remove temporary dashboard scaffold {}",
                staging.display()
            )
        })?;
    }
    result?;

    // If another launcher won the race, its directory remains untouched.
    Ok(published)
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
            "could not publish dashboard scaffold at {}",
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
            "could not publish dashboard scaffold at {}",
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
        Err(error) => Err(error).with_context(|| {
            format!(
                "could not publish dashboard scaffold at {}",
                destination.display()
            )
        }),
    }
}

fn write_scaffold_file(path: &Path, content: &str) -> Result<()> {
    let mut file = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(path)
        .with_context(|| {
            format!(
                "could not create dashboard scaffold file {}",
                path.display()
            )
        })?;
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

fn validate_uv(path: &Path) -> Result<PathBuf> {
    let path = if path.is_absolute() {
        path.to_path_buf()
    } else {
        env::current_dir()
            .context("could not read current directory")?
            .join(path)
    };
    let metadata =
        fs::metadata(&path).with_context(|| format!("could not inspect {}", path.display()))?;
    if !metadata.is_file() {
        bail!("{} is not a file", path.display());
    }

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if metadata.permissions().mode() & 0o111 == 0 {
            bail!("{} is not executable", path.display());
        }
    }

    Ok(path.canonicalize().unwrap_or(path))
}

fn find_on_path(name: &str) -> Option<PathBuf> {
    env::var_os("PATH").and_then(|path| {
        env::split_paths(&path)
            .map(|directory| directory.join(name))
            .find(|candidate| candidate.is_file())
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scaffolds_sql_only_when_creating_a_new_tools_directory() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let paths = AppPaths {
            database: directory.path().join("data.db"),
            tools: directory.path().join("nested").join("tools"),
            dashboard: directory.path().join("nested").join("dashboard"),
            workspace: directory.path().join("nested").join("workspace"),
            runtime: directory.path().join("nested").join("runtime"),
        };

        let prepared = prepare_directories(&paths).expect("prepare new directories");
        assert!(prepared.tools_scaffolded);
        assert!(prepared.dashboard_scaffolded);
        let sql = paths.tools.join("execute_sql.py");
        assert!(sql.is_file());
        fs::write(&sql, "# user-owned\n").expect("replace scaffold as user");

        let prepared = prepare_directories(&paths).expect("prepare existing directories");
        assert!(!prepared.tools_scaffolded);
        assert!(!prepared.dashboard_scaffolded);
        assert_eq!(
            fs::read_to_string(sql).expect("read user tool"),
            "# user-owned\n"
        );
    }

    #[test]
    fn resolves_defaults_beside_the_executable_root() {
        let root = Path::new("/tmp/lite-mcp-dist");
        let paths = resolve_paths(root, None, None, None, None, None).expect("resolve paths");
        assert_eq!(paths.database, root.join("data.db"));
        assert_eq!(paths.tools, root.join("tools"));
        assert_eq!(paths.dashboard, root.join("dashboard"));
        assert_eq!(paths.workspace, root.join("workspace"));
        assert_eq!(paths.runtime, root.join("runtime"));
    }

    #[test]
    fn resolves_relative_dashboard_override_from_the_current_directory() {
        let root = Path::new("/tmp/lite-mcp-dist");
        let cwd = env::current_dir().expect("read current directory");
        let paths = resolve_paths(
            root,
            None,
            None,
            Some(PathBuf::from("custom-dashboard")),
            None,
            None,
        )
        .expect("resolve paths");
        assert_eq!(paths.dashboard, cwd.join("custom-dashboard"));
    }

    #[test]
    fn dashboard_scaffold_is_complete_and_never_repairs_an_existing_directory() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let paths = AppPaths {
            database: directory.path().join("data.db"),
            tools: directory.path().join("tools"),
            dashboard: directory.path().join("dashboard"),
            workspace: directory.path().join("workspace"),
            runtime: directory.path().join("runtime"),
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
            tools: directory.path().join("tools"),
            dashboard: directory.path().join("dashboard"),
            workspace: directory.path().join("workspace"),
            runtime: directory.path().join("runtime"),
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
