use std::{path::PathBuf, sync::Arc};

use rmcp::{
    ErrorData as McpError, ServerHandler,
    handler::server::{router::tool::ToolRouter, wrapper::Parameters},
    model::{CallToolResult, Implementation, ServerCapabilities, ServerInfo},
    schemars, tool, tool_handler, tool_router,
};
use serde::Deserialize;
use serde_json::json;

use crate::db;

#[derive(Debug, Deserialize, schemars::JsonSchema)]
pub struct ExecuteSqlRequest {
    /// One SQLite statement to execute against the lite-mcp database.
    pub sql: String,
}

#[derive(Clone)]
pub struct SqlMcpServer {
    database_path: Arc<PathBuf>,
    #[allow(dead_code)]
    tool_router: ToolRouter<Self>,
}

#[tool_router]
impl SqlMcpServer {
    pub fn new(database_path: Arc<PathBuf>) -> Self {
        Self {
            database_path,
            tool_router: Self::tool_router(),
        }
    }

    #[tool(
        description = "Execute one SQLite statement against the local lite-mcp database. SELECT, PRAGMA, and RETURNING statements return at most 100 rows as structured JSON. Mutating statements return affected_rows and last_insert_rowid. This server has no authentication; INSERT, UPDATE, DELETE, DDL, and PRAGMA statements can permanently change the database."
    )]
    async fn execute_sql(
        &self,
        Parameters(ExecuteSqlRequest { sql }): Parameters<ExecuteSqlRequest>,
    ) -> Result<CallToolResult, McpError> {
        let database_path = Arc::clone(&self.database_path);
        let outcome = tokio::task::spawn_blocking(move || db::execute_sql(&database_path, &sql))
            .await
            .map_err(|error| {
                McpError::internal_error(format!("SQL worker failed: {error}"), None)
            })?;

        match outcome {
            Ok(result) => serde_json::to_value(result)
                .map(CallToolResult::structured)
                .map_err(|error| {
                    McpError::internal_error(
                        format!("could not serialize SQL result: {error}"),
                        None,
                    )
                }),
            Err(error) => Ok(CallToolResult::structured_error(json!({
                "error": error.to_string()
            }))),
        }
    }
}

#[tool_handler]
impl ServerHandler for SqlMcpServer {
    fn get_info(&self) -> ServerInfo {
        let database_path = self.database_path.to_string_lossy();
        ServerInfo::new(ServerCapabilities::builder().enable_tools().build())
            .with_server_info(
                Implementation::new(env!("CARGO_PKG_NAME"), env!("CARGO_PKG_VERSION"))
                    .with_title("lite-mcp SQLite")
                    .with_description("Local SQLite access over Model Context Protocol"),
            )
            .with_instructions(format!(
                "Use execute_sql to inspect or modify the SQLite database at {database_path}. Prefer read-only SELECT queries unless the user explicitly asks for a mutation. The server returns at most 100 rows per query. This MCP endpoint has no authentication and should only be exposed on a trusted network."
            ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn tool_executes_sql_and_reports_errors() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let path = directory.path().join("test.db");
        db::initialize(&path).expect("initialize database");
        let server = SqlMcpServer::new(Arc::new(path));

        let success = server
            .execute_sql(Parameters(ExecuteSqlRequest {
                sql: "SELECT 42 AS answer".into(),
            }))
            .await
            .expect("tool request succeeds");
        assert_eq!(success.is_error, Some(false));
        assert_eq!(success.structured_content.unwrap()["rows"][0][0], 42);

        let failure = server
            .execute_sql(Parameters(ExecuteSqlRequest {
                sql: "SELECT FROM invalid".into(),
            }))
            .await
            .expect("tool returns caller-visible failure");
        assert_eq!(failure.is_error, Some(true));
        assert!(failure.structured_content.unwrap()["error"].is_string());
    }
}
