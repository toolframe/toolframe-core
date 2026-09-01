use std::{
    collections::BTreeSet,
    io::Write,
    net::{IpAddr, Ipv4Addr, SocketAddr},
    path::{Path, PathBuf},
    sync::Arc,
};

use anyhow::{Context, Result};
use axum::{Json, Router, extract::State, http::StatusCode, response::Html, routing::get};
use clap::Parser;
use lite_mcp::{db, mcp::SqlMcpServer};
use rmcp::transport::streamable_http_server::{
    StreamableHttpServerConfig, StreamableHttpService, session::local::LocalSessionManager,
};
use serde::Serialize;
use tokio::net::TcpListener;
use tokio_util::sync::CancellationToken;
use tracing::{error, info, warn};
use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt};

const INDEX_HTML: &str = include_str!("../static/index.html");
const DEFAULT_HOST: IpAddr = IpAddr::V4(Ipv4Addr::UNSPECIFIED);

#[derive(Debug, Parser)]
#[command(version, about)]
struct Cli {
    /// Interface to listen on. The default exposes the server to the LAN.
    #[arg(long, env = "LITE_MCP_HOST", default_value_t = DEFAULT_HOST)]
    host: IpAddr,

    /// Preferred port. If unavailable, nearby ports are tried; 0 lets the OS choose.
    #[arg(long, env = "LITE_MCP_PORT", default_value_t = 8000)]
    port: u16,

    /// Number of ports to try after the preferred port before asking the OS.
    #[arg(long, env = "LITE_MCP_PORT_SEARCH_SPAN", default_value_t = 100)]
    port_search_span: u16,

    /// SQLite database path.
    #[arg(long, env = "LITE_MCP_DB", default_value = "data.db")]
    database: PathBuf,
}

#[derive(Clone)]
struct AppState {
    database_path: Arc<PathBuf>,
}

#[derive(Debug, Serialize)]
struct ErrorBody {
    error: String,
}

#[tokio::main]
async fn main() -> Result<()> {
    initialize_logging();
    let cli = Cli::parse();

    db::initialize(&cli.database)
        .with_context(|| format!("failed to initialize {}", cli.database.display()))?;
    let database_path = Arc::new(
        cli.database
            .canonicalize()
            .unwrap_or_else(|_| cli.database.clone()),
    );

    let listener = bind_available(cli.host, cli.port, cli.port_search_span).await?;
    let address = listener
        .local_addr()
        .context("could not read bound address")?;
    let cancellation_token = CancellationToken::new();
    let allowed_hosts = allowed_hosts(cli.host);
    let allowed_origins = allowed_origins(cli.host, address.port());

    let mcp_database_path = Arc::clone(&database_path);
    let mcp_service = StreamableHttpService::new(
        move || Ok(SqlMcpServer::new(Arc::clone(&mcp_database_path))),
        LocalSessionManager::default().into(),
        StreamableHttpServerConfig::default()
            .with_legacy_session_mode(false)
            .with_json_response(true)
            .with_allowed_hosts(allowed_hosts)
            .with_allowed_origins(allowed_origins)
            .with_cancellation_token(cancellation_token.child_token()),
    );

    let state = AppState {
        database_path: Arc::clone(&database_path),
    };
    let app = Router::new()
        .route("/", get(index))
        .route("/healthz", get(health))
        .route("/api/database", get(database_snapshot))
        .nest_service("/mcp", mcp_service)
        .with_state(state);

    announce(address, &database_path);

    let shutdown_token = cancellation_token.clone();
    axum::serve(listener, app)
        .with_graceful_shutdown(async move {
            shutdown_signal().await;
            shutdown_token.cancel();
        })
        .await
        .context("HTTP server failed")?;

    info!("server stopped");
    Ok(())
}

fn initialize_logging() {
    tracing_subscriber::registry()
        .with(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "lite_mcp=info,tower_http=info".into()),
        )
        .with(tracing_subscriber::fmt::layer())
        .init();
}

async fn index() -> Html<&'static str> {
    Html(INDEX_HTML)
}

async fn health() -> &'static str {
    "ok"
}

async fn database_snapshot(
    State(state): State<AppState>,
) -> Result<Json<db::DatabaseSnapshot>, (StatusCode, Json<ErrorBody>)> {
    let path = Arc::clone(&state.database_path);
    match tokio::task::spawn_blocking(move || db::snapshot(&path)).await {
        Ok(Ok(snapshot)) => Ok(Json(snapshot)),
        Ok(Err(error)) => {
            error!(%error, "database preview failed");
            Err((
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(ErrorBody {
                    error: error.to_string(),
                }),
            ))
        }
        Err(error) => {
            error!(%error, "database preview worker failed");
            Err((
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(ErrorBody {
                    error: "database preview worker failed".into(),
                }),
            ))
        }
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

fn allowed_hosts(bind_host: IpAddr) -> Vec<String> {
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

fn allowed_origins(bind_host: IpAddr, port: u16) -> Vec<String> {
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

fn http_origin(address: IpAddr, port: u16) -> String {
    match address {
        IpAddr::V4(address) => format!("http://{address}:{port}"),
        IpAddr::V6(address) => format!("http://[{address}]:{port}"),
    }
}

fn announce(address: SocketAddr, database_path: &Path) {
    let port = address.port();
    let primary_ip = match address.ip() {
        IpAddr::V4(ip) if ip.is_unspecified() => IpAddr::V4(Ipv4Addr::LOCALHOST),
        IpAddr::V6(ip) if ip.is_unspecified() => IpAddr::V6(std::net::Ipv6Addr::LOCALHOST),
        ip => ip,
    };
    let local_url = http_origin(primary_ip, port);

    println!(
        "lite-mcp {version} is ready",
        version = env!("CARGO_PKG_VERSION")
    );
    println!("Dashboard: {local_url}/");
    println!("MCP:       {local_url}/mcp");
    println!("Database:  {}", database_path.display());

    if address.ip() == IpAddr::V4(Ipv4Addr::UNSPECIFIED)
        && let Ok(interfaces) = local_ip_address::list_afinet_netifas()
    {
        let lan_urls = interfaces
            .into_iter()
            .map(|(_, ip)| ip)
            .filter(|ip| ip.is_ipv4() && !ip.is_loopback())
            .map(|ip| format!("http://{ip}:{port}"))
            .collect::<BTreeSet<_>>();
        for url in lan_urls {
            println!("LAN:       {url}/");
        }
    }

    println!("WARNING: no authentication; SQL access is trusted-network only.");
    println!("LITE_MCP_URL={local_url}");
    println!("LITE_MCP_MCP_URL={local_url}/mcp");
    let _ = std::io::stdout().flush();
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

#[cfg(test)]
mod tests {
    use super::*;
    use axum::response::IntoResponse;

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
    async fn accepts_os_assigned_port() {
        let selected = bind_available(IpAddr::V4(Ipv4Addr::LOCALHOST), 0, 0)
            .await
            .expect("select OS-assigned port");
        assert_ne!(selected.local_addr().expect("selected address").port(), 0);
    }

    #[test]
    fn allows_loopback_and_interface_hosts() {
        let hosts = allowed_hosts(DEFAULT_HOST);
        assert!(hosts.iter().any(|host| host == "localhost"));
        assert!(hosts.iter().any(|host| host == "127.0.0.1"));
    }

    #[test]
    fn allows_only_local_origins() {
        let origins = allowed_origins(DEFAULT_HOST, 8123);
        assert!(
            origins
                .iter()
                .any(|origin| origin == "http://localhost:8123")
        );
        assert!(
            origins
                .iter()
                .any(|origin| origin == "http://127.0.0.1:8123")
        );
        assert!(!origins.iter().any(|origin| origin.contains("example.com")));
    }

    #[test]
    fn error_body_is_an_http_response() {
        let response = (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(ErrorBody {
                error: "failure".into(),
            }),
        )
            .into_response();
        assert_eq!(response.status(), StatusCode::INTERNAL_SERVER_ERROR);
    }
}
