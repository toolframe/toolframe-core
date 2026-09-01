# lite-mcp

`lite-mcp` is a zero-config SQLite dashboard and Model Context Protocol server packaged as one Rust executable. It chooses an available port, listens on the LAN by default, embeds its web UI, and exposes a single `execute_sql` MCP tool.

> [!WARNING]
> The MVP intentionally has no authentication and the SQL tool can permanently modify or delete database content. Run it only on a trusted network and stop it when it is not in use.

## Quick start

Build and run from the repository root:

```sh
cargo run --release
```

The first run creates `data.db` in the current directory. The server prefers port `8000`, tries the next 100 ports when necessary, and finally asks the operating system for a free port. It prints the exact URLs it selected:

```text
lite-mcp 0.1.0 is ready
Dashboard: http://127.0.0.1:8000/
MCP:       http://127.0.0.1:8000/mcp
LAN:       http://192.168.1.20:8000/
```

After building, the executable at `target/release/lite-mcp` has no Python, `uv`, or system SQLite runtime dependency.

## Connect Codex

Use the MCP URL printed at startup. For example:

```sh
codex mcp add lite-mcp --url http://127.0.0.1:8000/mcp
codex mcp list
```

Or add the same URL to `~/.codex/config.toml` (or a trusted project's `.codex/config.toml`):

```toml
[mcp_servers.lite_mcp]
url = "http://127.0.0.1:8000/mcp"
```

If port `8000` was occupied, use the actual `LITE_MCP_MCP_URL` printed by the executable. Codex connects through the current Streamable HTTP transport; the legacy `/mcp/sse` endpoint is not used.

## What is exposed

| Route | Purpose |
| --- | --- |
| `GET /` | Embedded database dashboard |
| `GET /api/database` | JSON table metadata and bounded row previews |
| `GET /healthz` | Lightweight health check |
| `/mcp` | Unauthenticated Streamable HTTP MCP endpoint |

The MCP server advertises one tool:

```text
execute_sql(sql: string)
```

It accepts one SQLite statement. Queries return structured column/row data capped at 100 rows. Mutations return `affected_rows` and `last_insert_rowid`. SQL failures are returned as caller-visible MCP tool errors instead of crashing the server.

The dashboard previews up to 25 rows per table. All database-provided values are inserted into the page as text, so stored HTML is displayed rather than executed.

## Configuration

```text
--host <IP>                 Interface to bind [default: 0.0.0.0]
--port <PORT>               Preferred port; 0 asks the OS [default: 8000]
--port-search-span <COUNT>  Nearby ports to try [default: 100]
--database <PATH>           SQLite database [default: data.db]
```

The corresponding environment variables are `LITE_MCP_HOST`, `LITE_MCP_PORT`, `LITE_MCP_PORT_SEARCH_SPAN`, and `LITE_MCP_DB`.

For loopback-only use:

```sh
./target/release/lite-mcp --host 127.0.0.1 --port 8000 --database ./data.db
```

## Architecture and MVP boundary

- Axum serves the embedded HTML dashboard and JSON preview API.
- The official Rust MCP SDK (`rmcp`) serves a stateless Streamable HTTP endpoint.
- `rusqlite` with bundled SQLite owns migrations, previews, and tool execution.
- The listener itself performs race-free port selection by keeping the first successful socket bound.
- MCP requests validate local/LAN `Host` values and reject non-local browser `Origin` values.

This MVP deliberately leaves authentication/TLS, OS sandboxing, dynamic Python tool discovery, PEP 723 execution, and database permission policies for later iterations. The current SQL tool is powerful by design and is not a security boundary.

## Development

```sh
cargo fmt --all -- --check
cargo test --all-targets
cargo clippy --all-targets -- -D warnings
cargo build --release
```

The implementation follows the [MCP Streamable HTTP transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports#streamable-http) and Codex's [MCP configuration](https://learn.chatgpt.com/docs/extend/mcp) model.
