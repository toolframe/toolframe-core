#!/usr/bin/env bash
set -euo pipefail
export MACOSX_DEPLOYMENT_TARGET=12.0
toolframe_workspace="$(cd "$(dirname "$0")/../.." && pwd)"
toolframe_core="$toolframe_workspace/toolframe-core"
toolframe_bundle="$toolframe_core/build/bootstrap-wheels"
mkdir -p "$toolframe_bundle"
for repository in toolframe-sdk fastapi-cordis-reactive toolframe-core; do
  uv build --no-sources --wheel --out-dir "$toolframe_bundle" "$toolframe_workspace/$repository"
done
TOOLFRAME_BUNDLE_DIR="$toolframe_bundle" cargo build --manifest-path "$toolframe_core/Cargo.toml" --release "$@"
