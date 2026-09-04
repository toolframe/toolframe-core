#!/usr/bin/env bash
set -euo pipefail
export LANG=C
export LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MINIMUM_MACOS_VERSION="12.0"
PACKAGE_VERSION="$(
  sed -n '/^\[package\]/,/^\[/s/^version = "\([^"]*\)"/\1/p' \
    "$REPOSITORY_ROOT/Cargo.toml" | head -n 1
)"
RELEASE_STAGING="$(mktemp -d "${TMPDIR:-/tmp}/toolframe-release.XXXXXX")"

cleanup() {
  rm -rf "$RELEASE_STAGING"
}
trap cleanup EXIT

if [[ "$#" -eq 0 ]]; then
  RELEASE_TARGETS=(aarch64-apple-darwin x86_64-apple-darwin)
else
  RELEASE_TARGETS=("$@")
fi

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "error: Apple releases must be built on macOS with Apple's SDK" >&2
  exit 1
fi
if [[ -z "$PACKAGE_VERSION" ]]; then
  echo "error: could not read the package version from Cargo.toml" >&2
  exit 1
fi
for command_name in cargo lipo otool rustup shasum; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "error: required command is not available: $command_name" >&2
    exit 1
  fi
done

# cross-rs cannot distribute Apple/Darwin images or the Apple SDK. Building
# Apple target triples with Cargo on macOS uses the installed, licensed SDK and
# still cross-compiles between Apple Silicon and Intel.
echo "Building Apple target releases with Cargo and the installed Apple SDK."

SEEN_TARGETS=" "
for RUST_TARGET in "${RELEASE_TARGETS[@]}"; do
  case "$RUST_TARGET" in
    aarch64-apple-darwin)
      MACH_ARCH="arm64"
      RELEASE_ARCH="arm64"
      ;;
    x86_64-apple-darwin)
      MACH_ARCH="x86_64"
      RELEASE_ARCH="x86_64"
      ;;
    *)
      echo "error: unsupported release target: $RUST_TARGET" >&2
      echo "supported targets: aarch64-apple-darwin x86_64-apple-darwin" >&2
      exit 1
      ;;
  esac
  case "$SEEN_TARGETS" in
    *" $RUST_TARGET "*)
      echo "error: duplicate release target: $RUST_TARGET" >&2
      exit 1
      ;;
  esac
  SEEN_TARGETS="$SEEN_TARGETS$RUST_TARGET "

  RELEASE_NAME="toolframe-v$PACKAGE_VERSION-macos-12-$RELEASE_ARCH"
  TARGET_DIST="$REPOSITORY_ROOT/dist/$RUST_TARGET"
  DESTINATION="$TARGET_DIST/$RELEASE_NAME"
  TARGET_STAGING="$RELEASE_STAGING/$RUST_TARGET"
  STAGED_DESTINATION="$TARGET_STAGING/$RELEASE_NAME"

  if [[ -e "$TARGET_DIST" || -L "$TARGET_DIST" ]]; then
    echo "error: deployment directory $TARGET_DIST already exists; move it aside before creating this release" >&2
    exit 1
  fi

  mkdir -p "$TARGET_STAGING"
  rustup target add "$RUST_TARGET"

  MACOSX_DEPLOYMENT_TARGET="$MINIMUM_MACOS_VERSION" \
    CARGO_TARGET="$RUST_TARGET" \
    DESTINATION="$STAGED_DESTINATION" \
    "$SCRIPT_DIR/package.sh"

  if [[ ! -f "$STAGED_DESTINATION" || ! -x "$STAGED_DESTINATION" || -L "$STAGED_DESTINATION" ]]; then
    echo "error: package did not produce exactly one regular executable at $STAGED_DESTINATION" >&2
    exit 1
  fi

  if [[ "$(lipo -archs "$STAGED_DESTINATION")" != "$MACH_ARCH" ]]; then
    echo "error: packaged toolframe executable has the wrong architecture for $RUST_TARGET" >&2
    exit 1
  fi
  TOOLFRAME_MINIMUM_MACOS="$(
    otool -l "$STAGED_DESTINATION" |
      awk '
        $1 == "cmd" && ($2 == "LC_BUILD_VERSION" || $2 == "LC_VERSION_MIN_MACOSX") { active = 1; next }
        active && ($1 == "minos" || $1 == "version") { print $2; exit }
      '
  )"
  if [[ "$TOOLFRAME_MINIMUM_MACOS" != "$MINIMUM_MACOS_VERSION" ]]; then
    echo "error: toolframe deployment target is $TOOLFRAME_MINIMUM_MACOS, expected $MINIMUM_MACOS_VERSION" >&2
    exit 1
  fi

  RELEASE_SHA256="$(shasum -a 256 "$STAGED_DESTINATION" | awk '{print $1}')"

  mkdir -p "$(dirname "$TARGET_DIST")"
  mkdir "$TARGET_DIST"
  mv "$STAGED_DESTINATION" "$DESTINATION"
  RELEASE_ENTRY_COUNT="$(find "$TARGET_DIST" -mindepth 1 -maxdepth 1 -print | wc -l | tr -d ' ')"
  if [[ "$RELEASE_ENTRY_COUNT" != "1" ]]; then
    echo "error: deployment directory must contain exactly one entry, found $RELEASE_ENTRY_COUNT" >&2
    exit 1
  fi

  echo "Created $RUST_TARGET single-binary release: $DESTINATION"
  echo "SHA-256: $RELEASE_SHA256"
done
