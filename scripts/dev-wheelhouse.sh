#!/usr/bin/env bash
set -euo pipefail
toolframe_workspace="$(cd "$(dirname "$0")/../.." && pwd)"
toolframe_wheels="$toolframe_workspace/toolframe-core/wheelhouse"
mkdir -p "$toolframe_wheels"
for repository in toolframe-sdk fastapi-cordis toolframe-sql-sqlite toolframe-llms-txt toolframe-fastmcp toolframe-streamlit-dashboard toolframe-demo-entry-register toolframe-auth toolframe-dashboard-auth toolframe-mcp-auth toolframe-oidc-auth toolframe-core; do
  uv build --no-sources --wheel --out-dir "$toolframe_wheels" "$toolframe_workspace/$repository"
done
