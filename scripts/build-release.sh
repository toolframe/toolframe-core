#!/usr/bin/env bash
# Local thin executable. For platform wheels and Intel companion pins use
# scripts/build-launcher.py; no Python package is embedded by either workflow.
set -euo pipefail
toolframe_core="$(cd "$(dirname "$0")/.." && pwd)"
export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-12.0}"
cargo build --manifest-path "$toolframe_core/Cargo.toml" --release --locked "$@"
