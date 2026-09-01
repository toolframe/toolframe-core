use std::{
    collections::{BTreeSet, VecDeque},
    io::Write,
    net::{IpAddr, Ipv4Addr, SocketAddr},
    path::{Path, PathBuf},
    process::Stdio,
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use anyhow::{Context, Result, anyhow, bail};
use clap::Parser;
use lite_mcp::{db, runtime, tools};
use tokio::{
    io::{AsyncBufReadExt, BufReader},
    net::TcpListener,
    process::Command,
    sync::{Mutex, oneshot},
    time,
};
use tracing::{info, warn};
use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt};

const DEFAULT_HOST: IpAddr = IpAddr::V4(Ipv4Addr::UNSPECIFIED);
const READY_SENTINEL: &str = "LITE_MCP_READY";
const FATAL_SENTINEL: &str = "LITE_MCP_FATAL ";
const STDERR_HISTORY_LINES: usize = 80;
const STDERR_DRAIN_TIMEOUT: Duration = Duration::from_secs(2);

#[derive(Debug, Default)]
struct StderrState {
    history: VecDeque<String>,
    fatal: Option<FatalDiagnostic>,
}

#[derive(Debug, Clone, PartialEq)]
struct FatalDiagnostic {
    raw_line: String,
    payload: FatalPayload,
}

#[derive(Debug, Clone, PartialEq)]
enum FatalPayload {
    Json(serde_json::Value),
    Malformed(String),
}

impl FatalDiagnostic {
    fn summary(&self) -> String {
        let payload = match &self.payload {
            FatalPayload::Json(payload) => payload,
            FatalPayload::Malformed(error) => {
                return format!(
                    "malformed dashboard fatal sentinel: {error}; raw line: {}",
                    self.raw_line
                );
            }
        };

        let field = |name: &str| payload.get(name).and_then(serde_json::Value::as_str);
        let mut details = Vec::new();
        for (label, field_name) in [
            ("traceId", "traceId"),
            ("phase", "phase"),
            ("path", "path"),
            ("widget", "widget"),
            ("exceptionType", "exceptionType"),
        ] {
            if let Some(value) = field(field_name) {
                details.push(format!("{label}={value}"));
            }
        }
        if let Some(retryable) = payload
            .get("retryable")
            .and_then(serde_json::Value::as_bool)
        {
            details.push(format!("retryable={retryable}"));
        }
        let context = if details.is_empty() {
            String::new()
        } else {
            format!(" ({})", details.join(", "))
        };
        let message = field("message")
            .map(str::to_owned)
            .unwrap_or_else(|| payload.to_string());
        format!("dashboard fatal error{context}: {message}")
    }
}

#[derive(Debug, Parser)]
#[command(version, about)]
struct Cli {
    /// Interface to listen on. The default intentionally exposes the server to the LAN.
    #[arg(long, env = "LITE_MCP_HOST", default_value_t = DEFAULT_HOST)]
    host: IpAddr,

    /// Preferred port. Busy ports are skipped; 0 asks the OS for any available port.
    #[arg(long, env = "LITE_MCP_PORT", default_value_t = 8000)]
    port: u16,

    /// Number of sequential ports to try before asking the OS for a free port.
    #[arg(long, env = "LITE_MCP_PORT_SEARCH_SPAN", default_value_t = 100)]
    port_search_span: u16,

    /// SQLite database path. Relative overrides are resolved from the current directory.
    #[arg(long, env = "LITE_MCP_DB")]
    database: Option<PathBuf>,

    /// Startup-only Python tool directory.
    #[arg(long, env = "LITE_MCP_TOOLS")]
    tools_dir: Option<PathBuf>,

    /// Editable Streamlit dashboard directory. Source changes load on browser rerun.
    #[arg(long, env = "LITE_MCP_DASHBOARD_DIR")]
    dashboard_dir: Option<PathBuf>,

    /// Working directory and trusted read/write area for Python tools.
    #[arg(long, env = "LITE_MCP_WORKSPACE")]
    workspace_dir: Option<PathBuf>,

    /// Private UV project, Python installation, lock, and cache directory.
    #[arg(long, env = "LITE_MCP_RUNTIME")]
    runtime_dir: Option<PathBuf>,

    /// UV executable. Resolution order is this override, bundled sibling, then PATH.
    #[arg(long, env = "LITE_MCP_UV")]
    uv_path: Option<PathBuf>,

    /// Resolve only from artifacts already available to UV.
    #[arg(long, env = "LITE_MCP_OFFLINE", default_value_t = false)]
    offline: bool,

    /// Concurrent tool calls. The default is 60; explicit 0 means unlimited.
    #[arg(long, env = "LITE_MCP_MAX_CONCURRENCY", default_value_t = 60)]
    max_concurrency: u32,

    /// Best-effort timeout for each tool call in seconds; 0 disables it.
    #[arg(long, env = "LITE_MCP_TOOL_TIMEOUT", default_value_t = 60.0, value_parser = finite_nonnegative)]
    tool_timeout: f64,

    /// Maximum encoded MCP tool result size in bytes.
    #[arg(long, env = "LITE_MCP_RESULT_LIMIT", default_value_t = 4_194_304, value_parser = positive_usize)]
    result_limit: usize,

    /// Maximum time for Python, dependency resolution, and strict tool loading.
    #[arg(long, env = "LITE_MCP_STARTUP_TIMEOUT", default_value_t = 600)]
    startup_timeout: u64,
}

#[tokio::main]
async fn main() -> Result<()> {
    initialize_logging();
    let cli = Cli::parse();
    run(cli).await
}

async fn run(cli: Cli) -> Result<()> {
    let executable_root = runtime::executable_root()?;
    let paths = runtime::resolve_paths(
        &executable_root,
        cli.database,
        cli.tools_dir,
        cli.dashboard_dir,
        cli.workspace_dir,
        cli.runtime_dir,
    )?;
    let directories = runtime::prepare_directories(&paths)?;
    if directories.tools_scaffolded {
        info!(path = %paths.tools.display(), "created tools directory and SQL reference tool");
    }
    if directories.dashboard_scaffolded {
        info!(path = %paths.dashboard.display(), "created editable Streamlit dashboard scaffold");
    }

    db::initialize(&paths.database)
        .with_context(|| format!("failed to initialize {}", paths.database.display()))?;
    let database = paths.database.canonicalize().with_context(|| {
        format!(
            "could not resolve database path {}",
            paths.database.display()
        )
    })?;
    let tools_dir = paths
        .tools
        .canonicalize()
        .with_context(|| format!("could not resolve tools path {}", paths.tools.display()))?;
    let dashboard_dir = paths.dashboard.canonicalize().with_context(|| {
        format!(
            "could not resolve dashboard path {}",
            paths.dashboard.display()
        )
    })?;
    let workspace = paths.workspace.canonicalize().with_context(|| {
        format!(
            "could not resolve workspace path {}",
            paths.workspace.display()
        )
    })?;
    let runtime_dir = paths
        .runtime
        .canonicalize()
        .with_context(|| format!("could not resolve runtime path {}", paths.runtime.display()))?;

    let catalog = tools::discover(&tools_dir)?;
    let dashboard_manifest_path = dashboard_dir.join("pyproject.toml");
    let dashboard_manifest = tools::inspect_dashboard_manifest(&dashboard_manifest_path)?;
    let startup_dependency_context =
        format_startup_dependency_context(&catalog, &dashboard_manifest, &dashboard_manifest_path);
    let prepared = runtime::prepare_runtime(&runtime_dir, &catalog, &dashboard_manifest)?;
    let uv = runtime::locate_uv(cli.uv_path.as_deref(), &executable_root)?;

    let listener = bind_available(cli.host, cli.port, cli.port_search_span).await?;
    let address = listener
        .local_addr()
        .context("could not read the selected listen address")?;
    let standard_listener = listener
        .into_std()
        .context("could not prepare the inherited HTTP listener")?;
    let listen_fd = inherited_listener_fd(&standard_listener)?;

    let allowed_hosts = runtime::allowed_hosts(cli.host);
    let allowed_origins = runtime::allowed_origins(cli.host, address.port());
    let dependency_json = serde_json::to_string(&prepared.dependencies)
        .context("could not encode dependency summary")?;
    let ready_token = tools::content_hash(format!(
        "{}:{}:{}",
        std::process::id(),
        prepared.manifest.display(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos()
    ));
    let ready_line = format!("{READY_SENTINEL} {ready_token}");

    let mut command = Command::new(&uv);
    command.arg("--no-config");
    if cli.offline {
        command.arg("--offline");
    }
    command
        .arg("run")
        .arg("--project")
        .arg(&prepared.project)
        .arg("--python")
        .arg("3.14")
        .arg("python")
        .arg(&prepared.core_server)
        .current_dir(&workspace)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .env("PYTHONUNBUFFERED", "1")
        .env("UV_MANAGED_PYTHON", "1")
        .env("UV_CACHE_DIR", runtime_dir.join("cache"))
        .env("UV_PYTHON_INSTALL_DIR", runtime_dir.join("python"))
        .env("LITE_MCP_DB", &database)
        .env("LITE_MCP_TOOLS", &tools_dir)
        .env("LITE_MCP_DASHBOARD_DIR", &dashboard_dir)
        .env("LITE_MCP_WORKSPACE", &workspace)
        .env("LITE_MCP_RUNTIME", &runtime_dir)
        .env("LITE_MCP_MANIFEST", &prepared.manifest)
        .env("LITE_MCP_INFO_PAGE", &prepared.info_page)
        .env("LITE_MCP_DASHBOARD_RUNNER", &prepared.dashboard_runner)
        .env("LITE_MCP_LISTEN_FD", listen_fd.to_string())
        .env("LITE_MCP_READY_TOKEN", ready_token)
        .env("LITE_MCP_HOST", cli.host.to_string())
        .env("LITE_MCP_PORT", address.port().to_string())
        .env(
            "LITE_MCP_ALLOWED_HOSTS",
            serde_json::to_string(&allowed_hosts).context("could not encode allowed hosts")?,
        )
        .env(
            "LITE_MCP_ALLOWED_ORIGINS",
            serde_json::to_string(&allowed_origins).context("could not encode allowed origins")?,
        )
        .env("LITE_MCP_DEPENDENCIES_JSON", dependency_json)
        .env("LITE_MCP_MAX_CONCURRENCY", cli.max_concurrency.to_string())
        .env(
            "LITE_MCP_TOOL_TIMEOUT_SECONDS",
            cli.tool_timeout.to_string(),
        )
        .env("LITE_MCP_RESULT_LIMIT_BYTES", cli.result_limit.to_string())
        .env("LITE_MCP_VERSION", env!("CARGO_PKG_VERSION"));

    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.as_std_mut().process_group(0);
    }

    let mut child = command
        .spawn()
        .with_context(|| format!("could not start UV at {}", uv.display()))?;
    drop(standard_listener);
    let child_pid = child
        .id()
        .ok_or_else(|| anyhow!("UV child process did not report a process id"))?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| anyhow!("could not capture UV stdout"))?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| anyhow!("could not capture UV stderr"))?;

    let stderr_state = Arc::new(Mutex::new(StderrState::default()));
    let (ready_sender, mut ready_receiver) = oneshot::channel();
    tokio::spawn(forward_stdout(stdout, ready_sender, ready_line));
    let mut stderr_task = tokio::spawn(forward_stderr(stderr, Arc::clone(&stderr_state)));

    let mut wait = Box::pin(child.wait());
    let mut deadline = Box::pin(time::sleep(Duration::from_secs(cli.startup_timeout)));
    tokio::select! {
        ready = &mut ready_receiver => {
            if ready.is_err() {
                let status = match time::timeout(Duration::from_secs(2), &mut wait).await {
                    Ok(status) => status.context("could not wait for UV during startup")?,
                    Err(_) => {
                        terminate_process_group(child_pid, libc::SIGTERM);
                        match time::timeout(Duration::from_secs(5), &mut wait).await {
                            Ok(status) => status.context("could not wait for UV during startup")?,
                            Err(_) => {
                                terminate_process_group(child_pid, libc::SIGKILL);
                                wait.await.context("could not wait for UV after forced startup termination")?
                            }
                        }
                    }
                };
                drain_stderr_task(&mut stderr_task).await;
                let (fatal, diagnostics) = stderr_snapshot(&stderr_state).await;
                let fatal = format_fatal_context(fatal.as_ref());
                bail!("Python runtime exited before reporting readiness ({status}){fatal}{diagnostics}{startup_dependency_context}");
            }
        }
        status = &mut wait => {
            let status = status.context("could not wait for UV during startup")?;
            drain_stderr_task(&mut stderr_task).await;
            let (fatal, diagnostics) = stderr_snapshot(&stderr_state).await;
            let fatal = format_fatal_context(fatal.as_ref());
            bail!("Python runtime exited before startup completed ({status}){fatal}{diagnostics}{startup_dependency_context}");
        }
        _ = &mut deadline => {
            terminate_process_group(child_pid, libc::SIGTERM);
            match time::timeout(Duration::from_secs(5), &mut wait).await {
                Ok(status) => {
                    status.context("could not wait for UV after startup timeout")?;
                }
                Err(_) => {
                    terminate_process_group(child_pid, libc::SIGKILL);
                    wait.await.context("could not wait for UV after startup timeout")?;
                }
            }
            drain_stderr_task(&mut stderr_task).await;
            let (fatal, diagnostics) = stderr_snapshot(&stderr_state).await;
            let fatal = format_fatal_context(fatal.as_ref());
            bail!("Python runtime did not become ready within {} seconds{fatal}{diagnostics}{startup_dependency_context}", cli.startup_timeout);
        }
    }

    announce(
        address,
        &database,
        &tools_dir,
        &dashboard_dir,
        catalog.manifest.tools.len(),
    );

    let (status, shutdown_requested) = tokio::select! {
        status = &mut wait => {
            let status = status.context("could not wait for the Python runtime")?;
            (status, false)
        }
        _ = shutdown_signal() => {
            info!("shutdown requested; stopping Python runtime");
            terminate_process_group(child_pid, libc::SIGTERM);
            let status = match time::timeout(Duration::from_secs(5), &mut wait).await {
                Ok(status) => status.context("could not wait for the Python runtime during shutdown")?,
                Err(_) => {
                    warn!("Python runtime did not stop in 5 seconds; forcing termination");
                    terminate_process_group(child_pid, libc::SIGKILL);
                    wait.await.context("could not wait for the Python runtime after forced shutdown")?
                }
            };
            (status, true)
        }
    };

    drain_stderr_task(&mut stderr_task).await;
    let (fatal, diagnostics) = stderr_snapshot(&stderr_state).await;
    if let Some(fatal) = fatal {
        bail!(
            "Python runtime reported {} before stopping ({status}){diagnostics}",
            fatal.summary()
        );
    }
    if !shutdown_requested && !status.success() {
        bail!("Python runtime stopped unexpectedly ({status}){diagnostics}");
    }

    info!("server stopped");
    Ok(())
}

async fn forward_stdout(
    stdout: tokio::process::ChildStdout,
    ready_sender: oneshot::Sender<()>,
    ready_line: String,
) {
    let mut lines = BufReader::new(stdout).lines();
    let mut ready_sender = Some(ready_sender);
    while let Ok(Some(line)) = lines.next_line().await {
        if line.trim() == ready_line {
            if let Some(sender) = ready_sender.take() {
                let _ = sender.send(());
            }
        } else {
            println!("{line}");
        }
    }
}

async fn forward_stderr(stderr: tokio::process::ChildStderr, state: Arc<Mutex<StderrState>>) {
    let mut lines = BufReader::new(stderr).lines();
    while let Ok(Some(line)) = lines.next_line().await {
        eprintln!("{line}");
        let fatal = parse_fatal_sentinel(&line);
        let mut state = state.lock().await;
        if state.fatal.is_none()
            && let Some(fatal) = fatal
        {
            state.fatal = Some(fatal);
        }
        state.history.push_back(line);
        while state.history.len() > STDERR_HISTORY_LINES {
            state.history.pop_front();
        }
    }
}

fn parse_fatal_sentinel(line: &str) -> Option<FatalDiagnostic> {
    let encoded = line.strip_prefix(FATAL_SENTINEL)?;
    let payload = match serde_json::from_str::<serde_json::Value>(encoded) {
        Ok(payload) if payload.is_object() => FatalPayload::Json(payload),
        Ok(_) => FatalPayload::Malformed("payload must be a JSON object".to_owned()),
        Err(error) => FatalPayload::Malformed(format!("invalid JSON: {error}")),
    };
    Some(FatalDiagnostic {
        raw_line: line.to_owned(),
        payload,
    })
}

fn format_fatal_context(fatal: Option<&FatalDiagnostic>) -> String {
    fatal
        .map(|fatal| format!("\nFatal diagnostic: {}", fatal.summary()))
        .unwrap_or_default()
}

async fn stderr_snapshot(state: &Mutex<StderrState>) -> (Option<FatalDiagnostic>, String) {
    let state = state.lock().await;
    let diagnostics = if state.history.is_empty() {
        String::new()
    } else {
        format!(
            "\nRecent Python/UV diagnostics:\n{}",
            state.history.iter().cloned().collect::<Vec<_>>().join("\n")
        )
    };
    (state.fatal.clone(), diagnostics)
}

async fn drain_stderr_task(task: &mut tokio::task::JoinHandle<()>) {
    if time::timeout(STDERR_DRAIN_TIMEOUT, &mut *task)
        .await
        .is_err()
    {
        warn!("timed out while draining Python stderr; preserving collected diagnostics");
        task.abort();
        let _ = task.await;
    }
}

async fn bind_available(
    host: IpAddr,
    preferred_port: u16,
    search_span: u16,
) -> Result<TcpListener> {
    if preferred_port == 0 {
        return TcpListener::bind(SocketAddr::new(host, 0))
            .await
            .context("could not bind an OS-assigned port");
    }

    for offset in 0..=search_span {
        let Some(port) = preferred_port.checked_add(offset) else {
            break;
        };
        let address = SocketAddr::new(host, port);
        match TcpListener::bind(address).await {
            Ok(listener) => {
                if offset > 0 {
                    warn!(
                        preferred_port,
                        selected_port = port,
                        "preferred port was busy"
                    );
                }
                return Ok(listener);
            }
            Err(error) if error.kind() == std::io::ErrorKind::AddrInUse => continue,
            Err(error) => return Err(error).with_context(|| format!("could not bind {address}")),
        }
    }

    warn!(
        preferred_port,
        search_span, "configured port range was busy; asking the OS for a free port"
    );
    TcpListener::bind(SocketAddr::new(host, 0))
        .await
        .context("could not bind any available port")
}

#[cfg(unix)]
fn inherited_listener_fd(listener: &std::net::TcpListener) -> Result<i32> {
    use std::os::fd::AsRawFd;

    let fd = listener.as_raw_fd();
    // SAFETY: fcntl is called with a valid listener fd and does not outlive it.
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    if flags < 0 {
        return Err(std::io::Error::last_os_error()).context("could not inspect listener fd flags");
    }
    // SAFETY: this only clears close-on-exec for the validated listener fd.
    if unsafe { libc::fcntl(fd, libc::F_SETFD, flags & !libc::FD_CLOEXEC) } < 0 {
        return Err(std::io::Error::last_os_error()).context("could not make listener inheritable");
    }
    Ok(fd)
}

#[cfg(not(unix))]
fn inherited_listener_fd(_: &std::net::TcpListener) -> Result<i32> {
    bail!("inherited listener startup is currently supported on macOS and other Unix systems")
}

#[cfg(unix)]
fn terminate_process_group(pid: u32, signal: i32) {
    if let Ok(pid) = i32::try_from(pid) {
        // SAFETY: negative pid targets only the dedicated child process group.
        let result = unsafe { libc::kill(-pid, signal) };
        if result < 0 {
            let error = std::io::Error::last_os_error();
            if error.raw_os_error() != Some(libc::ESRCH) {
                warn!(%error, pid, signal, "could not signal Python process group");
            }
        }
    }
}

#[cfg(not(unix))]
fn terminate_process_group(_: u32, _: i32) {}

fn announce(
    address: SocketAddr,
    database: &Path,
    tools_dir: &Path,
    dashboard_dir: &Path,
    tool_count: usize,
) {
    let port = address.port();
    let primary_ip = match address.ip() {
        IpAddr::V4(ip) if ip.is_unspecified() => IpAddr::V4(Ipv4Addr::LOCALHOST),
        IpAddr::V6(ip) if ip.is_unspecified() => IpAddr::V6(std::net::Ipv6Addr::LOCALHOST),
        ip => ip,
    };
    let local_url = runtime::http_origin(primary_ip, port);

    println!("lite-mcp {} is ready", env!("CARGO_PKG_VERSION"));
    println!("Dashboard: {local_url}/ (Streamlit)");
    println!("Info:      {local_url}/info");
    println!("MCP:       {local_url}/mcp");
    println!("Database:  {}", database.display());
    println!("Tools:     {} ({tool_count} loaded)", tools_dir.display());
    println!("Dashboard files: {}", dashboard_dir.display());

    if address.ip().is_unspecified()
        && let Ok(interfaces) = local_ip_address::list_afinet_netifas()
    {
        let lan_urls = interfaces
            .into_iter()
            .map(|(_, ip)| ip)
            .filter(|ip| ip.is_ipv4() && !ip.is_loopback())
            .map(|ip| runtime::http_origin(ip, port))
            .collect::<BTreeSet<_>>();
        for url in lan_urls {
            println!("LAN:       {url}/");
        }
    }

    println!(
        "WARNING: no authentication; every LAN client can view Dashboard data, submit Dashboard writes, call trusted Python tools, and modify data."
    );
    println!("LITE_MCP_URL={local_url}");
    println!("LITE_MCP_MCP_URL={local_url}/mcp");
    let _ = std::io::stdout().flush();
}

fn format_startup_dependency_context(
    catalog: &tools::ToolCatalog,
    dashboard: &tools::DashboardManifest,
    dashboard_manifest_path: &Path,
) -> String {
    let dashboard_dependencies = if dashboard.dependencies.is_empty() {
        "none".to_owned()
    } else {
        dashboard.dependencies.join(", ")
    };
    let dashboard_declaration = format!(
        "\nDashboard dependency declaration:\n- {} (requires-python {}; dependencies: {})",
        dashboard_manifest_path.display(),
        dashboard.requires_python,
        dashboard_dependencies
    );

    if catalog.manifest.tools.is_empty() {
        return format!("{dashboard_declaration}\nStartup tool declarations: none");
    }

    let tool_declarations = catalog
        .manifest
        .tools
        .iter()
        .map(|tool| {
            let dependencies = if tool.dependencies.is_empty() {
                "none".to_owned()
            } else {
                tool.dependencies.join(", ")
            };
            format!(
                "- {} (requires-python {}; dependencies: {})",
                tool.path.display(),
                tool.requires_python,
                dependencies
            )
        })
        .collect::<Vec<_>>()
        .join("\n");
    format!("{dashboard_declaration}\nStartup tool declarations:\n{tool_declarations}")
}

fn initialize_logging() {
    tracing_subscriber::registry()
        .with(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "lite_mcp=info".into()),
        )
        .with(tracing_subscriber::fmt::layer())
        .init();
}

async fn shutdown_signal() {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{SignalKind, signal};

        let mut terminate = match signal(SignalKind::terminate()) {
            Ok(signal) => signal,
            Err(error) => {
                warn!(%error, "could not install SIGTERM handler");
                let _ = tokio::signal::ctrl_c().await;
                return;
            }
        };
        tokio::select! {
            result = tokio::signal::ctrl_c() => {
                if let Err(error) = result {
                    warn!(%error, "Ctrl-C handler failed");
                }
            }
            _ = terminate.recv() => {}
        }
    }

    #[cfg(not(unix))]
    if let Err(error) = tokio::signal::ctrl_c().await {
        warn!(%error, "Ctrl-C handler failed");
    }
}

fn finite_nonnegative(value: &str) -> std::result::Result<f64, String> {
    let value: f64 = value.parse().map_err(|_| "must be a number".to_owned())?;
    if value.is_finite() && value >= 0.0 {
        Ok(value)
    } else {
        Err("must be a finite non-negative number".to_owned())
    }
}

fn positive_usize(value: &str) -> std::result::Result<usize, String> {
    let value: usize = value
        .parse()
        .map_err(|_| "must be a positive integer".to_owned())?;
    if value > 0 {
        Ok(value)
    } else {
        Err("must be greater than zero".to_owned())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn skips_an_occupied_preferred_port() {
        let occupied = TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
            .await
            .expect("bind occupied port");
        let occupied_port = occupied.local_addr().expect("occupied address").port();
        let selected = bind_available(IpAddr::V4(Ipv4Addr::LOCALHOST), occupied_port, 3)
            .await
            .expect("select fallback port");
        assert_ne!(
            selected.local_addr().expect("selected address").port(),
            occupied_port
        );
    }

    #[tokio::test]
    async fn accepts_an_os_assigned_port() {
        let selected = bind_available(IpAddr::V4(Ipv4Addr::LOCALHOST), 0, 0)
            .await
            .expect("select OS-assigned port");
        assert_ne!(selected.local_addr().expect("selected address").port(), 0);
    }

    #[test]
    fn validates_numeric_settings() {
        assert_eq!(finite_nonnegative("0").expect("zero timeout"), 0.0);
        assert!(finite_nonnegative("NaN").is_err());
        assert_eq!(positive_usize("1").expect("positive cap"), 1);
        assert!(positive_usize("0").is_err());
    }

    #[test]
    fn parses_structured_dashboard_fatal_sentinel() {
        let fatal = parse_fatal_sentinel(
            r#"LITE_MCP_FATAL {"traceId":"trace-42","phase":"render","path":"/dashboard/app.py","widget":"example_card","exceptionType":"RuntimeError","message":"boom","retryable":false}"#,
        )
        .expect("parse fatal sentinel");

        assert!(fatal.summary().contains("traceId=trace-42"));
        assert!(fatal.summary().contains("widget=example_card"));
        assert!(fatal.summary().contains("retryable=false"));
        assert!(fatal.summary().ends_with(": boom"));
    }

    #[test]
    fn malformed_fatal_sentinel_is_still_fatal_and_normal_lines_are_ignored() {
        let fatal =
            parse_fatal_sentinel("LITE_MCP_FATAL {not-json").expect("recognize malformed sentinel");
        assert!(matches!(fatal.payload, FatalPayload::Malformed(_)));
        assert!(fatal.summary().contains("malformed"));

        assert!(parse_fatal_sentinel("LITE_MCP_FATALITY {}").is_none());
        assert!(parse_fatal_sentinel("ordinary Python diagnostic").is_none());
    }
}
