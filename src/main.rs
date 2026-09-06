use std::{
    collections::VecDeque,
    fs,
    io::{IsTerminal, Write},
    net::{IpAddr, Ipv4Addr, SocketAddr},
    path::{Path, PathBuf},
    process::Stdio,
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use anyhow::{Context, Result, anyhow, bail};
use clap::{Parser, Subcommand, ValueEnum};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    net::TcpListener,
    process::Command,
    sync::{Mutex, oneshot},
    time,
};
use toolframe::{development, runtime, tools, uv};
use tracing::{Event, Subscriber, info, warn};
use tracing_subscriber::{
    fmt::{FmtContext, FormatEvent, FormatFields, format::Writer},
    registry::LookupSpan,
};
use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt};

const DEFAULT_HOST: IpAddr = IpAddr::V4(Ipv4Addr::UNSPECIFIED);
const READY_SENTINEL: &str = "TOOLFRAME_READY";
const FATAL_SENTINEL: &str = "TOOLFRAME_FATAL ";
const STDERR_HISTORY_LINES: usize = 80;
const STDERR_DRAIN_TIMEOUT: Duration = Duration::from_secs(2);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ConsoleCategory {
    Boot,
    Uv,
    Db,
    Python,
    Dashboard,
    Mcp,
    Network,
    Security,
    Skill,
    Shutdown,
}

impl ConsoleCategory {
    const fn label(self) -> &'static str {
        match self {
            Self::Boot => "BOOT",
            Self::Uv => "UV",
            Self::Db => "DB",
            Self::Python => "PYTHON",
            Self::Dashboard => "DASHBOARD",
            Self::Mcp => "MCP",
            Self::Network => "NETWORK",
            Self::Security => "SECURITY",
            Self::Skill => "SKILL",
            Self::Shutdown => "SHUTDOWN",
        }
    }

    const fn ansi(self) -> &'static str {
        match self {
            Self::Boot | Self::Network => "36",
            Self::Uv => "34",
            Self::Db => "35",
            Self::Python => "37",
            Self::Dashboard => "32",
            Self::Mcp => "33",
            Self::Security => "31",
            Self::Skill => "32",
            Self::Shutdown => "90",
        }
    }

    fn from_tracing_target(target: &str) -> Self {
        match target {
            "toolframe::uv" => Self::Uv,
            "toolframe::db" => Self::Db,
            "toolframe::python" => Self::Python,
            "toolframe::dashboard" => Self::Dashboard,
            "toolframe::mcp" => Self::Mcp,
            "toolframe::network" => Self::Network,
            "toolframe::security" => Self::Security,
            "toolframe::skill" => Self::Skill,
            "toolframe::shutdown" => Self::Shutdown,
            _ => Self::Boot,
        }
    }
}

#[derive(Debug, Clone, Copy)]
struct ConsoleStyle {
    color: bool,
}

impl ConsoleStyle {
    fn detect(no_color: bool) -> Self {
        Self {
            color: !no_color
                && std::env::var_os("NO_COLOR").is_none()
                && std::io::stderr().is_terminal(),
        }
    }

    fn format(self, category: ConsoleCategory, message: &str) -> String {
        format_console_line(category, message, self.color, &local_timestamp())
    }

    fn print(self, category: ConsoleCategory, message: impl AsRef<str>) {
        eprintln!("{}", self.format(category, message.as_ref()));
    }
}

#[derive(Debug, Clone, Copy)]
struct TracingConsoleFormatter {
    style: ConsoleStyle,
}

impl<S, N> FormatEvent<S, N> for TracingConsoleFormatter
where
    S: Subscriber + for<'lookup> LookupSpan<'lookup>,
    N: for<'writer> FormatFields<'writer> + 'static,
{
    fn format_event(
        &self,
        context: &FmtContext<'_, S, N>,
        mut writer: Writer<'_>,
        event: &Event<'_>,
    ) -> std::fmt::Result {
        let prefix = self.style.format(
            ConsoleCategory::from_tracing_target(event.metadata().target()),
            event.metadata().level().as_str(),
        );
        write!(writer, "{prefix} ")?;
        context
            .field_format()
            .format_fields(writer.by_ref(), event)?;
        writeln!(writer)
    }
}

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
                    "malformed runtime fatal sentinel: {error}; raw line: {}",
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
        format!("runtime fatal error{context}: {message}")
    }
}

#[derive(Debug, Parser)]
#[command(version, about)]
struct Cli {
    #[command(subcommand)]
    command: Option<CliCommand>,

    /// Interface to listen on. The default intentionally exposes the server to the LAN.
    #[arg(long, env = "TOOLFRAME_HOST", default_value_t = DEFAULT_HOST, global = true)]
    host: IpAddr,

    /// Preferred port. Busy ports are skipped; 0 asks the OS for any available port.
    #[arg(long, env = "TOOLFRAME_PORT", default_value_t = 8000, global = true)]
    port: u16,

    /// Number of sequential ports to try before asking the OS for a free port.
    #[arg(
        long,
        env = "TOOLFRAME_PORT_SEARCH_SPAN",
        default_value_t = 100,
        global = true
    )]
    port_search_span: u16,

    /// SQLite database path. Relative overrides are resolved from the current directory.
    #[arg(long, global = true)]
    database: Option<PathBuf>,

    /// Editable app source directory containing dashboard.py and llms.txt.
    #[arg(long, env = "TOOLFRAME_APP_DIR", global = true)]
    app_dir: Option<PathBuf>,

    /// Editable business domain source root.
    #[arg(long, env = "TOOLFRAME_DOMAINS_DIR", global = true)]
    domains_dir: Option<PathBuf>,

    /// Working directory and trusted read/write area for Python tools.
    #[arg(long, env = "TOOLFRAME_WORKSPACE", global = true)]
    workspace_dir: Option<PathBuf>,

    /// Private runtime state directory. Defaults to .toolframe beside the executable.
    #[arg(long, env = "TOOLFRAME_RUNTIME", global = true)]
    runtime_dir: Option<PathBuf>,

    /// Structured audit and MCP JSONL directory. Relative paths resolve from the executable root.
    #[arg(long, env = "TOOLFRAME_LOGS_DIR", global = true)]
    logs_dir: Option<PathBuf>,

    /// Number of UTC date buckets retained, including today.
    #[arg(long, env = "TOOLFRAME_LOG_RETENTION_DAYS", default_value_t = 14, value_parser = positive_u64, global = true)]
    log_retention_days: u64,

    /// Combined maximum size of managed audit and MCP JSONL files.
    #[arg(long, env = "TOOLFRAME_LOG_MAX_TOTAL_MIB", default_value_t = 256, value_parser = positive_u64, global = true)]
    log_max_total_mib: u64,

    /// Forward routine UV and Python diagnostics to the categorized console.
    #[arg(
        long,
        env = "TOOLFRAME_VERBOSE",
        default_value_t = false,
        global = true
    )]
    verbose: bool,

    /// Disable ANSI colors. NO_COLOR is also honored.
    #[arg(
        long,
        env = "TOOLFRAME_NO_COLOR",
        default_value_t = false,
        global = true
    )]
    no_color: bool,

    /// Explicit UV executable. When omitted, the pinned build is downloaded and managed.
    #[arg(long, env = "TOOLFRAME_UV", global = true)]
    uv_path: Option<PathBuf>,

    /// Disable UV, Python, and dependency downloads.
    #[arg(
        long,
        env = "TOOLFRAME_OFFLINE",
        default_value_t = false,
        global = true
    )]
    offline: bool,

    /// Concurrent tool calls. The default is 60; explicit 0 means unlimited.
    #[arg(
        long,
        env = "TOOLFRAME_MAX_CONCURRENCY",
        default_value_t = 60,
        global = true
    )]
    max_concurrency: u32,

    /// Best-effort timeout for each tool call in seconds; 0 disables it.
    #[arg(long, env = "TOOLFRAME_TOOL_TIMEOUT", default_value_t = 60.0, value_parser = finite_nonnegative, global = true)]
    tool_timeout: f64,

    /// Maximum encoded MCP tool result size in bytes.
    #[arg(long, env = "TOOLFRAME_RESULT_LIMIT", default_value_t = 4_194_304, value_parser = positive_usize, global = true)]
    result_limit: usize,

    /// Maximum time for Python, dependency resolution, and strict tool loading.
    #[arg(
        long,
        env = "TOOLFRAME_STARTUP_TIMEOUT",
        default_value_t = 600,
        global = true
    )]
    startup_timeout: u64,
}

#[derive(Debug, Subcommand)]
enum CliCommand {
    /// Run the dashboard and MCP server (the default when omitted).
    Serve,
    /// Validate migrations, dependencies, tools, and dashboard code without starting a server.
    Check {
        /// Output format for the validation report.
        #[arg(long, value_enum, default_value_t = CheckFormat::Human)]
        format: CheckFormat,
    },
    /// Invoke one startup-loaded tool locally without starting a server.
    Invoke {
        /// Startup catalog tool name.
        tool: String,
        /// JSON object containing tool arguments.
        #[arg(long, default_value = "{}")]
        json: String,
    },
    /// Create one domain-owned source without overwriting files.
    Scaffold {
        #[command(subcommand)]
        command: ScaffoldCommand,
    },
}

#[derive(Debug, Subcommand)]
enum ScaffoldCommand {
    /// Create a complete business domain.
    Domain { domain: String },
    /// Create a namespaced migration draft with SQLite and PostgreSQL variants.
    Migration { domain: String, description: String },
    /// Create a PEP 723 FastMCP tool module in one domain.
    Tool { domain: String, name: String },
    /// Create a Streamlit dashboard widget module in one domain.
    Widget { domain: String, name: String },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, ValueEnum)]
enum CheckFormat {
    Human,
    Json,
}

#[tokio::main]
async fn main() {
    let cli = Cli::parse();
    let console = ConsoleStyle::detect(cli.no_color);
    let machine_errors = matches!(
        cli.command,
        Some(CliCommand::Check {
            format: CheckFormat::Json
        }) | Some(CliCommand::Invoke { .. })
    );
    initialize_logging(console);
    match run(cli, console).await {
        Ok(0) => {}
        Ok(exit_code) => std::process::exit(exit_code),
        Err(error) => {
            let message = format_launcher_error(&error);
            if machine_errors {
                println!(
                    "{}",
                    serde_json::json!({
                        "version": 1,
                        "ok": false,
                        "error": { "message": message }
                    })
                );
            } else {
                console.print(ConsoleCategory::Boot, message);
            }
            std::process::exit(1);
        }
    }
}

async fn run(cli: Cli, console: ConsoleStyle) -> Result<i32> {
    match cli.command.as_ref() {
        None | Some(CliCommand::Serve) => run_server(cli, console).await.map(|()| 0),
        Some(CliCommand::Check { format }) => run_check(&cli, *format).await,
        Some(CliCommand::Invoke { tool, json }) => run_invoke(&cli, tool, json).await,
        Some(CliCommand::Scaffold { command }) => run_scaffold(&cli, command).await,
    }
}

async fn run_server(cli: Cli, console: ConsoleStyle) -> Result<()> {
    let executable_root = runtime::executable_root()?;
    let skill_installed = runtime::install_business_skill(&executable_root)?;
    if skill_installed {
        console.print(
            ConsoleCategory::Skill,
            format!(
                "installed bundled business skill at {}",
                executable_root
                    .join(".agents")
                    .join("skills")
                    .join("toolframe-business-app")
                    .display()
            ),
        );
    }
    let logs_dir = runtime::prepare_logs_dir(&executable_root, cli.logs_dir.as_deref())?;
    console.print(
        ConsoleCategory::Boot,
        format!("toolframe {} starting", env!("CARGO_PKG_VERSION")),
    );
    let paths = runtime::resolve_paths(
        &executable_root,
        cli.app_dir,
        cli.domains_dir,
        cli.workspace_dir,
        cli.runtime_dir,
    )?;
    let directories = runtime::prepare_directories(&paths)?;
    if directories.app_scaffolded {
        info!(target: "toolframe::dashboard", path = %paths.app.display(), "created editable app scaffold");
    }
    if directories.domains_scaffolded {
        info!(target: "toolframe::python", path = %paths.domains.display(), "created editable example business domain");
    }
    if directories.app_context_scaffolded {
        info!(target: "toolframe::skill", path = %paths.root.join("APP_CONTEXT.md").display(), "created master-agent app context");
    }
    if directories.domain_model_scaffolded {
        info!(target: "toolframe::skill", path = %paths.root.join("DOMAIN_MODEL.md").display(), "created master-agent domain model");
    }

    let domains_dir = paths
        .domains
        .canonicalize()
        .with_context(|| format!("could not resolve domains path {}", paths.domains.display()))?;
    let app_dir = paths
        .app
        .canonicalize()
        .with_context(|| format!("could not resolve app path {}", paths.app.display()))?;
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

    let catalog = tools::discover(&domains_dir)?;
    let app_manifest_path = app_dir.join("pyproject.toml");
    let app_manifest = tools::inspect_app_manifest(&app_manifest_path)?;
    let startup_dependency_context =
        format_startup_dependency_context(&catalog, &app_manifest, &app_manifest_path);
    let prepared = runtime::prepare_runtime(&runtime_dir, &catalog, &app_manifest)?;
    let resolved_uv = uv::resolve(cli.uv_path.as_deref(), &runtime_dir, cli.offline).await?;
    let uv = resolved_uv.path;
    let uv_version = resolved_uv.version;
    console.print(
        ConsoleCategory::Uv,
        format!("runtime prepared with {} (uv {uv_version})", uv.display()),
    );

    let listener = bind_available(cli.host, cli.port, cli.port_search_span).await?;
    let address = listener
        .local_addr()
        .context("could not read the selected listen address")?;
    let standard_listener = listener
        .into_std()
        .context("could not prepare the inherited HTTP listener")?;

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
    let (python_log_level, uvicorn_log_level) = child_log_levels(cli.verbose);
    command.arg("--no-config");
    if cli.offline {
        command.arg("--offline");
    }
    command.arg("run");
    configure_uv_run_platform(&mut command);
    command
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
        .env("PYTHONPYCACHEPREFIX", runtime_dir.join("pycache"))
        .env("UV_MANAGED_PYTHON", "1")
        .env("UV_CACHE_DIR", runtime_dir.join("cache"))
        .env("UV_PYTHON_INSTALL_DIR", runtime_dir.join("python"))
        .env("TOOLFRAME_APP_DIR", &app_dir)
        .env("TOOLFRAME_DOMAINS_DIR", &domains_dir)
        .env("TOOLFRAME_WORKSPACE", &workspace)
        .env("TOOLFRAME_RUNTIME", &runtime_dir)
        .env("TOOLFRAME_LOGS_DIR", &logs_dir)
        .env(
            "TOOLFRAME_LOG_RETENTION_DAYS",
            cli.log_retention_days.to_string(),
        )
        .env(
            "TOOLFRAME_LOG_MAX_TOTAL_MIB",
            cli.log_max_total_mib.to_string(),
        )
        .env("TOOLFRAME_LOG_LEVEL", python_log_level)
        .env("TOOLFRAME_UVICORN_LOG_LEVEL", uvicorn_log_level)
        .env("TOOLFRAME_MANIFEST", &prepared.manifest)
        .env("TOOLFRAME_INFO_PAGE", &prepared.info_page)
        .env("TOOLFRAME_DASHBOARD_RUNNER", &prepared.dashboard_runner)
        .env("TOOLFRAME_READY_TOKEN", ready_token)
        .env("TOOLFRAME_HOST", cli.host.to_string())
        .env("TOOLFRAME_PORT", address.port().to_string())
        .env(
            "TOOLFRAME_ALLOWED_HOSTS",
            serde_json::to_string(&allowed_hosts).context("could not encode allowed hosts")?,
        )
        .env(
            "TOOLFRAME_ALLOWED_ORIGINS",
            serde_json::to_string(&allowed_origins).context("could not encode allowed origins")?,
        )
        .env("TOOLFRAME_DEPENDENCIES_JSON", dependency_json)
        .env("TOOLFRAME_MAX_CONCURRENCY", cli.max_concurrency.to_string())
        .env(
            "TOOLFRAME_TOOL_TIMEOUT_SECONDS",
            cli.tool_timeout.to_string(),
        )
        .env("TOOLFRAME_RESULT_LIMIT_BYTES", cli.result_limit.to_string())
        .env("TOOLFRAME_VERSION", env!("CARGO_PKG_VERSION"));

    configure_database_environment(&mut command, &paths.root, cli.database.as_deref())?;
    let listener_guard = configure_server_listener(&mut command, standard_listener)?;
    configure_child_process(&mut command);

    let mut child = command
        .spawn()
        .with_context(|| format!("could not start UV at {}", uv.display()))?;
    drop(listener_guard);
    let process_tree = ChildProcessTree::attach(&child)?;
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
    tokio::spawn(forward_stdout(
        stdout,
        ready_sender,
        ready_line,
        Arc::clone(&stderr_state),
        console,
        cli.verbose,
    ));
    let mut stderr_task = tokio::spawn(forward_stderr(
        stderr,
        Arc::clone(&stderr_state),
        console,
        cli.verbose,
    ));
    console.print(ConsoleCategory::Python, "waiting for runtime readiness");

    let mut wait = Box::pin(child.wait());
    let mut deadline = Box::pin(time::sleep(Duration::from_secs(cli.startup_timeout)));
    tokio::select! {
        ready = &mut ready_receiver => {
            if ready.is_err() {
                let status = match time::timeout(Duration::from_secs(2), &mut wait).await {
                    Ok(status) => status.context("could not wait for UV during startup")?,
                    Err(_) => {
                        process_tree.terminate_gracefully();
                        match time::timeout(Duration::from_secs(5), &mut wait).await {
                            Ok(status) => status.context("could not wait for UV during startup")?,
                            Err(_) => {
                                process_tree.terminate_forcefully();
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
            process_tree.terminate_gracefully();
            match time::timeout(Duration::from_secs(5), &mut wait).await {
                Ok(status) => {
                    status.context("could not wait for UV after startup timeout")?;
                }
                Err(_) => {
                    process_tree.terminate_forcefully();
                    wait.await.context("could not wait for UV after startup timeout")?;
                }
            }
            drain_stderr_task(&mut stderr_task).await;
            let (fatal, diagnostics) = stderr_snapshot(&stderr_state).await;
            let fatal = format_fatal_context(fatal.as_ref());
            bail!("Python runtime did not become ready within {} seconds{fatal}{diagnostics}{startup_dependency_context}", cli.startup_timeout);
        }
    }

    announce(Announcement {
        address,
        domains_dir: &domains_dir,
        app_dir: &app_dir,
        tool_count: catalog.manifest.tools.len(),
        logs_dir: &logs_dir,
        retention_days: cli.log_retention_days,
        max_total_mib: cli.log_max_total_mib,
        console,
    });

    let (status, shutdown_requested) = tokio::select! {
        status = &mut wait => {
            let status = status.context("could not wait for the Python runtime")?;
            (status, false)
        }
        _ = shutdown_signal() => {
            console.print(ConsoleCategory::Shutdown, "shutdown requested; stopping Python runtime");
            process_tree.terminate_gracefully();
            let status = match time::timeout(Duration::from_secs(5), &mut wait).await {
                Ok(status) => status.context("could not wait for the Python runtime during shutdown")?,
                Err(_) => {
                    warn!(target: "toolframe::shutdown", "Python runtime did not stop in 5 seconds; forcing termination");
                    process_tree.terminate_forcefully();
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

    console.print(ConsoleCategory::Shutdown, "server stopped");
    Ok(())
}

struct DevelopmentRuntime {
    paths: runtime::AppPaths,
    catalog: tools::ToolCatalog,
    prepared: runtime::PreparedRuntime,
    uv: PathBuf,
    uv_version: String,
    workspace: PathBuf,
    runtime_dir: PathBuf,
}

async fn prepare_development_runtime(
    cli: &Cli,
    require_workspace: bool,
) -> Result<DevelopmentRuntime> {
    let executable_root = runtime::executable_root()?;
    let paths = resolve_cli_paths(cli, &executable_root)?;
    let domains_dir = paths
        .domains
        .canonicalize()
        .with_context(|| format!("could not resolve domains path {}", paths.domains.display()))?;
    let app_dir = paths
        .app
        .canonicalize()
        .with_context(|| format!("could not resolve app path {}", paths.app.display()))?;
    let workspace = if paths.workspace.is_dir() {
        paths.workspace.canonicalize().with_context(|| {
            format!(
                "could not resolve workspace path {}",
                paths.workspace.display()
            )
        })?
    } else if require_workspace {
        bail!(
            "workspace directory {} does not exist; create it before invoking tools",
            paths.workspace.display()
        );
    } else {
        paths.workspace.clone()
    };
    fs::create_dir_all(&paths.runtime).with_context(|| {
        format!(
            "could not create managed runtime directory {}",
            paths.runtime.display()
        )
    })?;
    let runtime_dir = paths
        .runtime
        .canonicalize()
        .with_context(|| format!("could not resolve runtime path {}", paths.runtime.display()))?;
    let catalog = tools::discover(&domains_dir)?;
    let app_manifest = tools::inspect_app_manifest(&app_dir.join("pyproject.toml"))?;
    let prepared = runtime::prepare_runtime(&runtime_dir, &catalog, &app_manifest)?;
    let resolved_uv = uv::resolve(cli.uv_path.as_deref(), &runtime_dir, cli.offline).await?;
    let uv = resolved_uv.path;
    let uv_version = resolved_uv.version;
    Ok(DevelopmentRuntime {
        paths,
        catalog,
        prepared,
        uv,
        uv_version,
        workspace,
        runtime_dir,
    })
}

async fn run_check(cli: &Cli, format: CheckFormat) -> Result<i32> {
    let development = prepare_development_runtime(cli, false).await?;
    let output = run_cli_python(cli, &development, "check", None, None).await?;
    if !output.status.success() {
        bail!(
            "Python preflight failed ({}): {}{}",
            output.status,
            String::from_utf8_lossy(&output.stdout).trim(),
            format_child_stderr(&output.stderr)
        );
    }
    let python: serde_json::Value = serde_json::from_slice(&output.stdout)
        .context("Python preflight did not return valid JSON")?;
    forward_child_stderr(&output.stderr)?;
    let report = serde_json::json!({
        "version": 1,
        "ok": true,
        "toolframeVersion": env!("CARGO_PKG_VERSION"),
        "uvVersion": development.uv_version,
        "pythonVersion": python.get("pythonVersion"),
        "agentDocuments": python
            .get("agentDocuments")
            .cloned()
            .unwrap_or_else(|| serde_json::json!([])),
        "paths": {
            "database": python["database"]["target"],
            "app": development.paths.app,
            "domains": development.paths.domains,
            "workspace": development.paths.workspace,
            "runtime": development.paths.runtime,
        },
        "database": python.get("database"),
        "migrations": python.get("migrations"),
        "tools": python.get("tools").cloned().unwrap_or_else(|| serde_json::json!([])),
        "widgets": python.get("widgets").cloned().unwrap_or_else(|| serde_json::json!([])),
        "plugins": python.get("plugins").cloned().unwrap_or_else(|| serde_json::json!([])),
        "pluginValidation": python.get("pluginValidation"),
        "dependencies": development.prepared.dependencies,
        "startupOnly": true,
        "listenerBound": false,
        "databaseMutated": false,
    });
    match format {
        CheckFormat::Json => println!(
            "{}",
            serde_json::to_string_pretty(&report).context("could not encode check report")?
        ),
        CheckFormat::Human => {
            println!("toolframe check passed");
            println!(
                "  migrations  {} total ({} pending; {})",
                python["migrations"]["total"],
                python["migrations"]["pending"]
                    .as_array()
                    .map_or(0, Vec::len),
                python["migrations"]["validationLevel"]
                    .as_str()
                    .unwrap_or("unknown")
            );
            println!("  tools       {}", development.catalog.manifest.tools.len());
            println!(
                "  widgets     {}",
                python
                    .get("widgets")
                    .and_then(serde_json::Value::as_array)
                    .map_or(0, Vec::len)
            );
            println!(
                "  agent docs  {}",
                python
                    .get("agentDocuments")
                    .and_then(serde_json::Value::as_array)
                    .map_or(0, Vec::len)
            );
            println!(
                "  runtime     Python {} / uv {}",
                python["pythonVersion"].as_str().unwrap_or("unknown"),
                development.uv_version
            );
            println!("  network     no listener bound");
            println!("  database    configured database was not changed");
        }
    }
    Ok(0)
}

async fn run_invoke(cli: &Cli, tool: &str, arguments: &str) -> Result<i32> {
    let parsed: serde_json::Value =
        serde_json::from_str(arguments).context("--json must contain valid JSON")?;
    if !parsed.is_object() {
        bail!("--json must contain a JSON object");
    }
    let encoded = serde_json::to_vec(&parsed).context("could not encode tool arguments")?;
    let development = prepare_development_runtime(cli, true).await?;
    let output = run_cli_python(cli, &development, "invoke", Some(tool), Some(&encoded)).await?;
    let stdout = String::from_utf8(output.stdout).context("tool runner output was not UTF-8")?;
    serde_json::from_str::<serde_json::Value>(stdout.trim())
        .context("tool runner did not return valid JSON")?;
    forward_child_stderr(&output.stderr)?;
    println!("{}", stdout.trim());
    match output.status.code() {
        Some(code @ 0..=2) => Ok(code),
        _ => bail!(
            "tool runner stopped unexpectedly ({}){}",
            output.status,
            format_child_stderr(&output.stderr)
        ),
    }
}

async fn run_scaffold(cli: &Cli, command: &ScaffoldCommand) -> Result<i32> {
    if let ScaffoldCommand::Migration {
        domain,
        description,
    } = command
    {
        let development = prepare_development_runtime(cli, false).await?;
        let options = serde_json::to_vec(&serde_json::json!({
            "domain": domain, "description": description,
        }))?;
        let output = run_cli_python(
            cli,
            &development,
            "scaffold-migration",
            None,
            Some(&options),
        )
        .await?;
        forward_child_stderr(&output.stderr)?;
        if !output.status.success() {
            bail!(
                "Migration scaffold failed: {}",
                String::from_utf8_lossy(&output.stdout).trim()
            );
        }
        let report: serde_json::Value = serde_json::from_slice(&output.stdout)?;
        println!("{}", serde_json::to_string_pretty(&report)?);
        return Ok(0);
    }
    let executable_root = runtime::executable_root()?;
    let paths = resolve_cli_paths(cli, &executable_root)?;
    let destination = match command {
        ScaffoldCommand::Domain { domain } => development::scaffold_domain(&paths.domains, domain)?,
        ScaffoldCommand::Migration { .. } => unreachable!("handled by Python"),
        ScaffoldCommand::Tool { domain, name } => {
            development::scaffold_tool(&paths.domains, domain, name)?
        }
        ScaffoldCommand::Widget { domain, name } => {
            development::scaffold_widget(&paths.domains, domain, name)?
        }
    };
    println!("{}", destination.display());
    Ok(0)
}

fn resolve_cli_paths(cli: &Cli, executable_root: &Path) -> Result<runtime::AppPaths> {
    runtime::resolve_paths(
        executable_root,
        cli.app_dir.clone(),
        cli.domains_dir.clone(),
        cli.workspace_dir.clone(),
        cli.runtime_dir.clone(),
    )
}

async fn run_cli_python(
    cli: &Cli,
    development: &DevelopmentRuntime,
    mode: &str,
    tool: Option<&str>,
    stdin: Option<&[u8]>,
) -> Result<std::process::Output> {
    let mut command = Command::new(&development.uv);
    command.arg("--no-config");
    if cli.offline {
        command.arg("--offline");
    }
    command.arg("run");
    configure_uv_run_platform(&mut command);
    command
        .arg("--project")
        .arg(&development.prepared.project)
        .arg("--python")
        .arg("3.14")
        .arg("python")
        .arg(&development.prepared.cli_runner)
        .arg(mode)
        .arg("--manifest")
        .arg(&development.prepared.manifest)
        .arg("--app")
        .arg(&development.paths.app)
        .arg("--domains")
        .arg(&development.paths.domains)
        .arg("--workspace")
        .arg(&development.workspace)
        .arg("--timeout")
        .arg(cli.tool_timeout.to_string())
        .arg("--result-limit")
        .arg(cli.result_limit.to_string())
        .current_dir(if development.workspace.is_dir() {
            &development.workspace
        } else {
            &development.runtime_dir
        })
        .stdin(if stdin.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .env("PYTHONUNBUFFERED", "1")
        .env(
            "PYTHONPYCACHEPREFIX",
            development.runtime_dir.join("pycache"),
        )
        .env("UV_MANAGED_PYTHON", "1")
        .env("UV_CACHE_DIR", development.runtime_dir.join("cache"))
        .env(
            "UV_PYTHON_INSTALL_DIR",
            development.runtime_dir.join("python"),
        )
        .env("TOOLFRAME_APP_DIR", &development.paths.app)
        .env("TOOLFRAME_DOMAINS_DIR", &development.paths.domains)
        .env("TOOLFRAME_WORKSPACE", &development.workspace)
        .env("TOOLFRAME_RUNTIME", &development.runtime_dir)
        .env("TOOLFRAME_MANIFEST", &development.prepared.manifest)
        .env(
            "TOOLFRAME_DEPENDENCIES_JSON",
            serde_json::to_string(&development.prepared.dependencies)
                .context("could not encode dependency summary")?,
        )
        .env("TOOLFRAME_MAX_CONCURRENCY", "1")
        .env(
            "TOOLFRAME_TOOL_TIMEOUT_SECONDS",
            cli.tool_timeout.to_string(),
        )
        .env("TOOLFRAME_RESULT_LIMIT_BYTES", cli.result_limit.to_string())
        .env("TOOLFRAME_VERSION", env!("CARGO_PKG_VERSION"));
    configure_database_environment(
        &mut command,
        &development.paths.root,
        cli.database.as_deref(),
    )?;
    if let Some(tool) = tool {
        command.arg("--tool").arg(tool);
    }
    configure_child_process(&mut command);
    let mut child = command
        .spawn()
        .with_context(|| format!("could not start UV at {}", development.uv.display()))?;
    let _process_tree = ChildProcessTree::attach(&child)?;
    if let Some(input) = stdin {
        let mut child_stdin = child
            .stdin
            .take()
            .ok_or_else(|| anyhow!("could not open tool runner stdin"))?;
        child_stdin
            .write_all(input)
            .await
            .context("could not send JSON arguments to tool runner")?;
        child_stdin
            .shutdown()
            .await
            .context("could not close tool runner stdin")?;
    }
    time::timeout(
        Duration::from_secs(cli.startup_timeout)
            .saturating_add(Duration::from_secs_f64(cli.tool_timeout.max(0.0))),
        child.wait_with_output(),
    )
    .await
    .context("development command timed out")?
    .context("could not wait for the development Python runner")
}

fn configure_database_environment(
    command: &mut Command,
    executable_root: &Path,
    cli_database: Option<&Path>,
) -> Result<()> {
    // Python owns provider choice, precedence, target validation and diagnostics.
    command
        .env("TOOLFRAME_EXECUTABLE_ROOT", executable_root)
        .env("TOOLFRAME_INVOCATION_CWD", std::env::current_dir()?)
        .env_remove("TOOLFRAME_CLI_DATABASE");
    if let Some(path) = cli_database {
        command.env("TOOLFRAME_CLI_DATABASE", path);
    }
    Ok(())
}

fn format_child_stderr(stderr: &[u8]) -> String {
    let stderr = String::from_utf8_lossy(stderr);
    let stderr = stderr.trim();
    if stderr.is_empty() {
        String::new()
    } else {
        format!("\n{stderr}")
    }
}

fn configure_uv_run_platform(command: &mut Command) {
    if let Some(target) = uv::current_target() {
        command.arg("--python-platform").arg(target);
        #[cfg(target_os = "macos")]
        command.env("MACOSX_DEPLOYMENT_TARGET", uv::MINIMUM_MACOS_VERSION);
    }
}

fn forward_child_stderr(stderr: &[u8]) -> Result<()> {
    if stderr.is_empty() {
        return Ok(());
    }
    let mut destination = std::io::stderr().lock();
    destination
        .write_all(stderr)
        .context("could not forward development runner diagnostics")?;
    destination
        .flush()
        .context("could not flush development runner diagnostics")
}

async fn forward_stdout(
    stdout: tokio::process::ChildStdout,
    ready_sender: oneshot::Sender<()>,
    ready_line: String,
    diagnostics: Arc<Mutex<StderrState>>,
    console: ConsoleStyle,
    verbose: bool,
) {
    let mut lines = BufReader::new(stdout).lines();
    let mut ready_sender = Some(ready_sender);
    while let Ok(Some(line)) = lines.next_line().await {
        if line.trim() == ready_line {
            if let Some(sender) = ready_sender.take() {
                let _ = sender.send(());
            }
        } else {
            let classified = classify_child_line(&line);
            if verbose || classified.visible {
                console.print(classified.category, child_console_message(&line));
            }
            let mut diagnostics = diagnostics.lock().await;
            diagnostics.history.push_back(line);
            while diagnostics.history.len() > STDERR_HISTORY_LINES {
                diagnostics.history.pop_front();
            }
        }
    }
}

async fn forward_stderr(
    stderr: tokio::process::ChildStderr,
    state: Arc<Mutex<StderrState>>,
    console: ConsoleStyle,
    verbose: bool,
) {
    let mut lines = BufReader::new(stderr).lines();
    let mut error_block = false;
    while let Ok(Some(line)) = lines.next_line().await {
        let fatal = parse_fatal_sentinel(&line);
        let classified = classify_child_line(&line);
        if classified.error {
            error_block = true;
        } else if error_block && is_routine_info(&line) {
            error_block = false;
        }
        // The fatal sentinel is an internal child/launcher protocol. The
        // launcher renders its parsed summary once instead of exposing raw
        // JSON as a second diagnostic line.
        if fatal.is_none() && (verbose || classified.visible || error_block) {
            console.print(classified.category, child_console_message(&line));
        }
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

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct ClassifiedLine {
    category: ConsoleCategory,
    visible: bool,
    error: bool,
}

fn classify_child_line(line: &str) -> ClassifiedLine {
    let lower = line.to_ascii_lowercase();
    let concise_uv_phase = is_concise_uv_phase(&lower);
    let category = if lower.contains("uv ")
        || lower.starts_with("using cpython")
        || lower.starts_with("creating virtual environment")
        || lower.starts_with("downloading ")
        || lower.starts_with("downloaded ")
        || lower.starts_with("resolved ")
        || lower.starts_with("installed ")
    {
        ConsoleCategory::Uv
    } else if lower.starts_with("[dashboard]")
        || lower.contains("streamlit")
        || lower.contains("dashboard")
    {
        ConsoleCategory::Dashboard
    } else if lower.starts_with("[mcp]")
        || lower.contains("mcp.server")
        || lower.contains("fastmcp")
        || lower.contains("/mcp")
    {
        ConsoleCategory::Mcp
    } else {
        ConsoleCategory::Python
    };
    let error = line.starts_with(FATAL_SENTINEL)
        || lower.contains("traceback")
        || lower.contains(" error")
        || lower.starts_with("error")
        || lower.contains("critical")
        || lower.contains("exception");
    let warning = lower.contains("warning") || lower.starts_with("warn");
    ClassifiedLine {
        category,
        visible: error || warning || concise_uv_phase,
        error,
    }
}

fn child_console_message(line: &str) -> &str {
    for prefix in ["[PYTHON] ", "[DASHBOARD] ", "[MCP] "] {
        if let Some(message) = line.strip_prefix(prefix) {
            return message;
        }
    }
    line
}

fn is_concise_uv_phase(lower: &str) -> bool {
    lower.starts_with("using cpython")
        || lower.starts_with("creating virtual environment")
        || lower.starts_with("downloading cpython")
        || lower.starts_with("downloaded cpython")
        || (lower.starts_with("resolved ") && lower.contains("package"))
        || (lower.starts_with("installed ") && lower.contains("package"))
}

fn child_log_levels(verbose: bool) -> (&'static str, &'static str) {
    if verbose {
        ("DEBUG", "debug")
    } else {
        ("WARNING", "warning")
    }
}

fn is_routine_info(line: &str) -> bool {
    let lower = line.to_ascii_lowercase();
    lower.contains(" info ") || lower.starts_with("info:")
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

fn format_launcher_error(error: &anyhow::Error) -> String {
    format!("fatal: {error:#}")
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
        warn!(target: "toolframe::python", "timed out while draining Python stderr; preserving collected diagnostics");
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
                    warn!(target: "toolframe::network",
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

    warn!(target: "toolframe::network",
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

#[cfg(unix)]
fn configure_server_listener(
    command: &mut Command,
    listener: std::net::TcpListener,
) -> Result<Option<std::net::TcpListener>> {
    command.env(
        "TOOLFRAME_LISTEN_FD",
        inherited_listener_fd(&listener)?.to_string(),
    );
    Ok(Some(listener))
}

#[cfg(not(unix))]
fn configure_server_listener(
    command: &mut Command,
    listener: std::net::TcpListener,
) -> Result<Option<std::net::TcpListener>> {
    // Windows sockets cannot be inherited as Unix file descriptors. Release
    // the selected port immediately before spawning Python, which binds the
    // same host and port from TOOLFRAME_HOST/TOOLFRAME_PORT.
    command.env_remove("TOOLFRAME_LISTEN_FD");
    drop(listener);
    Ok(None)
}

#[cfg(unix)]
fn configure_child_process(command: &mut Command) {
    use std::os::unix::process::CommandExt;

    command.kill_on_drop(true);
    command.as_std_mut().process_group(0);
}

#[cfg(windows)]
fn configure_child_process(command: &mut Command) {
    use std::os::windows::process::CommandExt;
    use windows_sys::Win32::System::Threading::CREATE_NEW_PROCESS_GROUP;

    command.kill_on_drop(true);
    command
        .as_std_mut()
        .creation_flags(CREATE_NEW_PROCESS_GROUP);
}

#[cfg(not(any(unix, windows)))]
fn configure_child_process(command: &mut Command) {
    command.kill_on_drop(true);
}

#[cfg(unix)]
struct ChildProcessTree {
    pid: u32,
}

#[cfg(unix)]
impl ChildProcessTree {
    fn attach(child: &tokio::process::Child) -> Result<Self> {
        let pid = child
            .id()
            .ok_or_else(|| anyhow!("UV child process did not report a process id"))?;
        Ok(Self { pid })
    }

    fn terminate_gracefully(&self) {
        self.signal(libc::SIGTERM);
    }

    fn terminate_forcefully(&self) {
        self.signal(libc::SIGKILL);
    }

    fn signal(&self, signal: i32) {
        let Ok(pid) = i32::try_from(self.pid) else {
            return;
        };
        // SAFETY: negative pid targets only the dedicated child process group.
        let result = unsafe { libc::kill(-pid, signal) };
        if result < 0 {
            let error = std::io::Error::last_os_error();
            if error.raw_os_error() != Some(libc::ESRCH) {
                warn!(target: "toolframe::shutdown", %error, pid = self.pid, signal, "could not signal Python process group");
            }
        }
    }
}

#[cfg(windows)]
struct ChildProcessTree {
    job: std::os::windows::io::OwnedHandle,
    pid: u32,
}

#[cfg(windows)]
impl ChildProcessTree {
    fn attach(child: &tokio::process::Child) -> Result<Self> {
        use std::{
            mem::size_of,
            os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle},
        };
        use windows_sys::Win32::{
            Foundation::HANDLE,
            System::JobObjects::{
                AssignProcessToJobObject, CreateJobObjectW, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
                JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JobObjectExtendedLimitInformation,
                SetInformationJobObject,
            },
        };

        let pid = child
            .id()
            .ok_or_else(|| anyhow!("UV child process did not report a process id"))?;
        // SAFETY: null security attributes and name create an unnamed job with
        // default access. The returned owned handle is closed exactly once.
        let raw_job = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if raw_job.is_null() {
            return Err(std::io::Error::last_os_error())
                .context("could not create a Windows process job for UV");
        }
        // SAFETY: CreateJobObjectW returned a fresh, non-null owned handle.
        let job = unsafe { OwnedHandle::from_raw_handle(raw_job as _) };
        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        // SAFETY: both handles remain valid for the call and `limits` has the
        // exact structure and byte length required by this information class.
        if unsafe {
            SetInformationJobObject(
                job.as_raw_handle() as HANDLE,
                JobObjectExtendedLimitInformation,
                std::ptr::from_ref(&limits).cast(),
                size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        } == 0
        {
            return Err(std::io::Error::last_os_error())
                .context("could not configure the Windows process job for UV");
        }
        let child_handle = child
            .raw_handle()
            .ok_or_else(|| anyhow!("UV child process exited before job assignment"))?;
        // SAFETY: the job and Tokio child process handles are valid and owned
        // by this process for the duration of the call.
        if unsafe {
            AssignProcessToJobObject(job.as_raw_handle() as HANDLE, child_handle as HANDLE)
        } == 0
        {
            return Err(std::io::Error::last_os_error())
                .context("could not attach UV to its Windows process job");
        }
        Ok(Self { job, pid })
    }

    fn terminate_gracefully(&self) {
        use windows_sys::Win32::System::Console::{CTRL_BREAK_EVENT, GenerateConsoleCtrlEvent};

        // SAFETY: configure_child_process created a process group whose id is
        // the UV process id. CTRL_BREAK is limited to that child group.
        if unsafe { GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, self.pid) } == 0 {
            let error = std::io::Error::last_os_error();
            warn!(target: "toolframe::shutdown", %error, pid = self.pid, "could not send Ctrl-Break to the Python process tree");
        }
    }

    fn terminate_forcefully(&self) {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::{Foundation::HANDLE, System::JobObjects::TerminateJobObject};

        // SAFETY: `job` is a live job handle owned by this value.
        if unsafe { TerminateJobObject(self.job.as_raw_handle() as HANDLE, 1) } == 0 {
            let error = std::io::Error::last_os_error();
            warn!(target: "toolframe::shutdown", %error, pid = self.pid, "could not terminate the Windows Python process job");
        }
    }
}

#[cfg(not(any(unix, windows)))]
struct ChildProcessTree;

#[cfg(not(any(unix, windows)))]
impl ChildProcessTree {
    fn attach(_: &tokio::process::Child) -> Result<Self> {
        Ok(Self)
    }

    fn terminate_gracefully(&self) {}

    fn terminate_forcefully(&self) {}
}

struct Announcement<'a> {
    address: SocketAddr,
    domains_dir: &'a Path,
    app_dir: &'a Path,
    tool_count: usize,
    logs_dir: &'a Path,
    retention_days: u64,
    max_total_mib: u64,
    console: ConsoleStyle,
}

fn announce(announcement: Announcement<'_>) {
    let Announcement {
        address,
        domains_dir,
        app_dir,
        tool_count,
        logs_dir,
        retention_days,
        max_total_mib,
        console,
    } = announcement;
    let port = address.port();
    let primary_ip = match address.ip() {
        IpAddr::V4(ip) if ip.is_unspecified() => IpAddr::V4(Ipv4Addr::LOCALHOST),
        IpAddr::V6(ip) if ip.is_unspecified() => IpAddr::V6(std::net::Ipv6Addr::LOCALHOST),
        ip => ip,
    };
    let local_url = runtime::http_origin(primary_ip, port);

    console.print(
        ConsoleCategory::Boot,
        format!("toolframe {} is ready", env!("CARGO_PKG_VERSION")),
    );
    console.print(
        ConsoleCategory::Dashboard,
        format!("Dashboard  {local_url}/ (Streamlit)"),
    );
    console.print(
        ConsoleCategory::Boot,
        format!("Info       {local_url}/info"),
    );
    console.print(ConsoleCategory::Mcp, format!("MCP        {local_url}/mcp"));
    console.print(
        ConsoleCategory::Mcp,
        format!("Agent guide {local_url}/llms.txt"),
    );
    console.print(
        ConsoleCategory::Db,
        format!("Database and schema ready; details at {local_url}/info"),
    );
    console.print(
        ConsoleCategory::Python,
        format!(
            "Domains    {} ({tool_count} tools loaded)",
            domains_dir.display()
        ),
    );
    console.print(
        ConsoleCategory::Dashboard,
        format!("App        {}", app_dir.display()),
    );
    console.print(
        ConsoleCategory::Security,
        format!(
            "JSONL logs {} ({retention_days} UTC days; {max_total_mib} MiB combined cap)",
            logs_dir.display()
        ),
    );

    if address.ip().is_unspecified()
        && let Ok(interfaces) = local_ip_address::list_afinet_netifas()
        && let Some(ip) = preferred_lan_address(interfaces.into_iter().map(|(_, ip)| ip))
    {
        let url = runtime::http_origin(ip, port);
        console.print(ConsoleCategory::Network, format!("LAN        {url}/"));
    }

    console.print(
        ConsoleCategory::Security,
        "WARNING no authentication: LAN clients can read and mutate data and call trusted tools",
    );
    console.print(
        ConsoleCategory::Security,
        "WARNING JSONL logs can contain unredacted SQL, parameters, payloads, results, and secrets",
    );
    for line in machine_stdout_lines(&local_url) {
        println!("{line}");
    }
    let _ = std::io::stdout().flush();
}

fn preferred_lan_address(addresses: impl IntoIterator<Item = IpAddr>) -> Option<IpAddr> {
    addresses
        .into_iter()
        .filter_map(|address| lan_address_rank(address).map(|rank| (rank, address)))
        .min_by_key(|(rank, address)| (*rank, *address))
        .map(|(_, address)| address)
}

fn lan_address_rank(address: IpAddr) -> Option<u8> {
    match address {
        IpAddr::V4(address) => {
            let octets = address.octets();
            let benchmark_network = octets[0] == 198 && matches!(octets[1], 18 | 19);
            if address.is_loopback()
                || address.is_unspecified()
                || address.is_link_local()
                || address.is_multicast()
                || address.is_broadcast()
                || octets[0] == 0
                || benchmark_network
            {
                None
            } else if address.is_private() {
                Some(0)
            } else {
                Some(1)
            }
        }
        IpAddr::V6(address) => {
            if address.is_loopback()
                || address.is_unspecified()
                || address.is_unicast_link_local()
                || address.is_multicast()
            {
                None
            } else if address.is_unique_local() {
                Some(2)
            } else {
                Some(3)
            }
        }
    }
}

fn machine_stdout_lines(local_url: &str) -> [String; 2] {
    [
        format!("TOOLFRAME_URL={local_url}"),
        format!("TOOLFRAME_MCP_URL={local_url}/mcp"),
    ]
}

fn format_startup_dependency_context(
    catalog: &tools::ToolCatalog,
    app: &tools::AppManifest,
    app_manifest_path: &Path,
) -> String {
    let app_dependencies = if app.dependencies.is_empty() {
        "none".to_owned()
    } else {
        app.dependencies.join(", ")
    };
    let app_declaration = format!(
        "\nApp dependency declaration:\n- {} (requires-python {}; dependencies: {})",
        app_manifest_path.display(),
        app.requires_python,
        app_dependencies
    );

    if catalog.manifest.tools.is_empty() {
        return format!("{app_declaration}\nStartup tool declarations: none");
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
    format!("{app_declaration}\nStartup tool declarations:\n{tool_declarations}")
}

fn initialize_logging(style: ConsoleStyle) {
    tracing_subscriber::registry()
        .with(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "toolframe=info".into()),
        )
        .with(
            tracing_subscriber::fmt::layer()
                .event_format(TracingConsoleFormatter { style })
                .with_ansi(false),
        )
        .init();
}

fn format_console_line(
    category: ConsoleCategory,
    message: &str,
    color: bool,
    timestamp: &str,
) -> String {
    if color {
        format!(
            "{timestamp} \x1b[{}m[{}]\x1b[0m {message}",
            category.ansi(),
            category.label()
        )
    } else {
        format!("{timestamp} [{}] {message}", category.label())
    }
}

#[cfg(unix)]
fn local_timestamp() -> String {
    let mut now = libc::timeval {
        tv_sec: 0,
        tv_usec: 0,
    };
    // SAFETY: now points to writable storage and a null timezone is supported.
    unsafe { libc::gettimeofday(&mut now, std::ptr::null_mut()) };
    let seconds = now.tv_sec;
    let mut local: libc::tm = unsafe { std::mem::zeroed() };
    // SAFETY: pointers are valid and localtime_r writes only to local.
    unsafe { libc::localtime_r(&seconds, &mut local) };
    format!(
        "{:02}:{:02}:{:02}.{:03}",
        local.tm_hour,
        local.tm_min,
        local.tm_sec,
        now.tv_usec / 1_000
    )
}

#[cfg(not(unix))]
fn local_timestamp() -> String {
    let elapsed = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    format!(
        "{:02}:{:02}:{:02}.{:03}",
        (elapsed.as_secs() / 3600) % 24,
        (elapsed.as_secs() / 60) % 60,
        elapsed.as_secs() % 60,
        elapsed.subsec_millis()
    )
}

async fn shutdown_signal() {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{SignalKind, signal};

        let mut terminate = match signal(SignalKind::terminate()) {
            Ok(signal) => signal,
            Err(error) => {
                warn!(target: "toolframe::shutdown", %error, "could not install SIGTERM handler");
                let _ = tokio::signal::ctrl_c().await;
                return;
            }
        };
        tokio::select! {
            result = tokio::signal::ctrl_c() => {
                if let Err(error) = result {
                    warn!(target: "toolframe::shutdown", %error, "Ctrl-C handler failed");
                }
            }
            _ = terminate.recv() => {}
        }
    }

    #[cfg(not(unix))]
    if let Err(error) = tokio::signal::ctrl_c().await {
        warn!(target: "toolframe::shutdown", %error, "Ctrl-C handler failed");
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

fn positive_u64(value: &str) -> std::result::Result<u64, String> {
    let value: u64 = value
        .parse()
        .map_err(|_| "must be a positive integer".to_owned())?;
    if value == 0 {
        Err("must be greater than zero".to_owned())
    } else {
        Ok(value)
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
        assert_eq!(positive_u64("14").expect("positive retention"), 14);
        assert!(positive_u64("0").is_err());
    }

    #[test]
    fn selects_one_private_ipv4_for_the_lan_announcement() {
        let addresses = [
            "198.18.0.1",
            "fdfe:dcba:9876::1",
            "fe80::1",
            "fe80::1bfd:500f:b70d:922b",
            "192.168.1.154",
        ]
        .map(|address| address.parse::<IpAddr>().expect("valid test address"));

        assert_eq!(
            preferred_lan_address(addresses),
            Some(IpAddr::V4(Ipv4Addr::new(192, 168, 1, 154)))
        );
    }

    #[test]
    fn ignores_unusable_lan_announcement_addresses() {
        let addresses = [
            "0.0.0.0",
            "127.0.0.1",
            "169.254.1.2",
            "198.18.0.1",
            "::",
            "::1",
            "fe80::1",
        ]
        .map(|address| address.parse::<IpAddr>().expect("valid test address"));

        assert_eq!(preferred_lan_address(addresses), None);
    }

    #[test]
    fn parses_logging_cli_settings_and_flags() {
        let cli = Cli::try_parse_from([
            "toolframe",
            "--logs-dir",
            "relative-logs",
            "--log-retention-days",
            "7",
            "--log-max-total-mib",
            "64",
            "--verbose",
            "--no-color",
        ])
        .expect("parse logging arguments");
        assert_eq!(cli.logs_dir, Some(PathBuf::from("relative-logs")));
        assert_eq!(cli.log_retention_days, 7);
        assert_eq!(cli.log_max_total_mib, 64);
        assert!(cli.verbose);
        assert!(cli.no_color);

        assert!(Cli::try_parse_from(["toolframe", "--log-retention-days", "0"]).is_err());
        assert!(Cli::try_parse_from(["toolframe", "--log-max-total-mib", "0"]).is_err());
    }

    #[test]
    fn parses_development_subcommands_with_shared_flags_after_the_command() {
        let check = Cli::try_parse_from([
            "toolframe",
            "check",
            "--format",
            "json",
            "--domains-dir",
            "custom-domains",
        ])
        .expect("parse check command");
        assert!(matches!(
            check.command,
            Some(CliCommand::Check {
                format: CheckFormat::Json
            })
        ));
        assert_eq!(check.domains_dir, Some(PathBuf::from("custom-domains")));

        let invoke = Cli::try_parse_from([
            "toolframe",
            "invoke",
            "echo",
            "--json",
            r#"{"value":"ok"}"#,
            "--tool-timeout",
            "3",
        ])
        .expect("parse invoke command");
        assert!(matches!(invoke.command, Some(CliCommand::Invoke { .. })));
        assert_eq!(invoke.tool_timeout, 3.0);

        let serve = Cli::try_parse_from(["toolframe", "serve", "--host", "127.0.0.1"])
            .expect("parse explicit serve command");
        assert!(matches!(serve.command, Some(CliCommand::Serve)));
        assert_eq!(serve.host, IpAddr::V4(Ipv4Addr::LOCALHOST));
    }

    #[test]
    fn parses_source_flags_and_resolves_executable_root_defaults() {
        let cli = Cli::try_parse_from([
            "toolframe",
            "--app-dir",
            "custom-app",
            "--domains-dir",
            "custom-domains",
        ])
        .expect("parse source flags");
        assert_eq!(cli.app_dir, Some(PathBuf::from("custom-app")));
        assert_eq!(cli.domains_dir, Some(PathBuf::from("custom-domains")));

        let default_cli = Cli::try_parse_from(["toolframe"]).expect("parse default cli");
        let root = Path::new("/tmp/toolframe-source-defaults");
        let paths = resolve_cli_paths(&default_cli, root).expect("resolve default paths");
        assert_eq!(paths.app, root.join("app"));
        assert_eq!(paths.domains, root.join("domains"));

        for legacy_flag in [
            "--dashboard-dir",
            "--tools-dir",
            "--migrations-dir",
            "--llms-file",
            "--llms-dir",
        ] {
            assert!(
                Cli::try_parse_from(["toolframe", legacy_flag, "legacy-source"]).is_err(),
                "legacy source flag must be rejected: {legacy_flag}"
            );
        }
    }

    #[test]
    fn formats_categorized_console_lines_with_optional_color() {
        assert_eq!(
            format_console_line(
                ConsoleCategory::Mcp,
                "request complete",
                false,
                "12:34:56.789"
            ),
            "12:34:56.789 [MCP] request complete"
        );
        let colored =
            format_console_line(ConsoleCategory::Security, "warning", true, "12:34:56.789");
        assert!(colored.contains("\x1b[31m[SECURITY]\x1b[0m"));
        assert!(colored.ends_with(" warning"));
        assert_eq!(
            ConsoleCategory::from_tracing_target("toolframe::network"),
            ConsoleCategory::Network
        );
        assert_eq!(
            ConsoleCategory::from_tracing_target("toolframe::shutdown"),
            ConsoleCategory::Shutdown
        );
        assert_eq!(
            ConsoleCategory::from_tracing_target("toolframe::skill"),
            ConsoleCategory::Skill
        );
        assert_eq!(
            format_console_line(
                ConsoleCategory::Skill,
                "installed bundled skill",
                false,
                "12:34:56.789"
            ),
            "12:34:56.789 [SKILL] installed bundled skill"
        );
        assert_eq!(
            ConsoleCategory::from_tracing_target("third_party"),
            ConsoleCategory::Boot
        );
    }

    #[test]
    fn classifies_child_sources_and_suppresses_routine_info() {
        let mcp = classify_child_line("2026-09-02 INFO mcp.server session started");
        assert_eq!(mcp.category, ConsoleCategory::Mcp);
        assert!(!mcp.visible);
        assert_eq!(
            classify_child_line("[MCP] ERROR mcp.shared request failed").category,
            ConsoleCategory::Mcp
        );
        assert_eq!(
            child_console_message("[DASHBOARD] ERROR streamlit render failed"),
            "ERROR streamlit render failed"
        );

        let uv = classify_child_line("Downloading streamlit (10 MiB)");
        assert_eq!(uv.category, ConsoleCategory::Uv);
        assert!(!uv.visible);

        for phase in [
            "Using CPython 3.14.7",
            "Creating virtual environment at: .venv",
            "Downloading cpython-3.14.7-macos-aarch64-none",
            "Downloaded cpython-3.14.7-macos-aarch64-none",
            "Resolved 90 packages in 120ms",
            "Installed 90 packages in 237ms",
        ] {
            let classified = classify_child_line(phase);
            assert_eq!(classified.category, ConsoleCategory::Uv, "{phase}");
            assert!(classified.visible, "{phase}");
        }

        assert!(!classify_child_line("Downloaded pygments").visible);

        let failure = classify_child_line("Traceback (most recent call last):");
        assert_eq!(failure.category, ConsoleCategory::Python);
        assert!(failure.visible);
        assert!(failure.error);
    }

    #[test]
    fn verbose_controls_python_emission_levels() {
        assert_eq!(child_log_levels(false), ("WARNING", "warning"));
        assert_eq!(child_log_levels(true), ("DEBUG", "debug"));
    }

    #[test]
    fn launcher_errors_preserve_the_full_anyhow_chain() {
        let error = anyhow!("root cause").context("startup failed");
        assert_eq!(
            format_launcher_error(&error),
            "fatal: startup failed: root cause"
        );
    }

    #[test]
    fn ready_stdout_contract_contains_only_machine_assignments() {
        assert_eq!(
            machine_stdout_lines("http://127.0.0.1:8000"),
            [
                "TOOLFRAME_URL=http://127.0.0.1:8000".to_owned(),
                "TOOLFRAME_MCP_URL=http://127.0.0.1:8000/mcp".to_owned(),
            ]
        );
    }

    #[test]
    fn parses_structured_dashboard_fatal_sentinel() {
        let fatal = parse_fatal_sentinel(
            r#"TOOLFRAME_FATAL {"traceId":"trace-42","phase":"render","path":"/app/dashboard.py","widget":"entry_register.entries","exceptionType":"RuntimeError","message":"boom","retryable":false}"#,
        )
        .expect("parse fatal sentinel");

        assert!(fatal.summary().contains("traceId=trace-42"));
        assert!(fatal.summary().contains("widget=entry_register.entries"));
        assert!(fatal.summary().contains("retryable=false"));
        assert!(fatal.summary().ends_with(": boom"));
    }

    #[test]
    fn malformed_fatal_sentinel_is_still_fatal_and_normal_lines_are_ignored() {
        let fatal = parse_fatal_sentinel("TOOLFRAME_FATAL {not-json")
            .expect("recognize malformed sentinel");
        assert!(matches!(fatal.payload, FatalPayload::Malformed(_)));
        assert!(fatal.summary().contains("malformed"));

        assert!(parse_fatal_sentinel("TOOLFRAME_FATALITY {}").is_none());
        assert!(parse_fatal_sentinel("ordinary Python diagnostic").is_none());
    }
}
