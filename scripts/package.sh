#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DESTINATION="${DESTINATION:-$REPOSITORY_ROOT/dist/lite-mcp}"
CARGO_TARGET="${CARGO_TARGET:-}"
STAGING_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/lite-mcp-package.XXXXXX")"

cleanup() {
  rm -rf "$STAGING_ROOT"
}
trap cleanup EXIT

if [[ -e "$DESTINATION" || -L "$DESTINATION" ]]; then
  echo "error: $DESTINATION already exists; move it aside before packaging" >&2
  exit 1
fi

cd "$REPOSITORY_ROOT"
CARGO_BUILD_ARGS=(build --locked --release)
if [[ -n "$CARGO_TARGET" ]]; then
  CARGO_BUILD_ARGS+=(--target "$CARGO_TARGET")
  RELEASE_BINARY="$REPOSITORY_ROOT/target/$CARGO_TARGET/release/lite-mcp"
else
  RELEASE_BINARY="$REPOSITORY_ROOT/target/release/lite-mcp"
fi
cargo "${CARGO_BUILD_ARGS[@]}"

if [[ ! -f "$RELEASE_BINARY" || ! -x "$RELEASE_BINARY" ]]; then
  echo "error: release binary is missing or not executable at $RELEASE_BINARY" >&2
  exit 1
fi

STAGED_BINARY="$STAGING_ROOT/lite-mcp"
install -m 0755 "$RELEASE_BINARY" "$STAGED_BINARY"
mkdir -p "$(dirname "$DESTINATION")"
mv "$STAGED_BINARY" "$DESTINATION"

echo "Packaged single-binary release: $DESTINATION"
echo "Run: $DESTINATION"
