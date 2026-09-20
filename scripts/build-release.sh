#!/usr/bin/env bash
set -euo pipefail
export MACOSX_DEPLOYMENT_TARGET=12.0
toolframe_workspace="$(cd "$(dirname "$0")/../.." && pwd)"
toolframe_core="$toolframe_workspace/toolframe-core"
toolframe_profile=standard
toolframe_native_wheels="$toolframe_core/wheelhouse"
toolframe_target="${CARGO_BUILD_TARGET:-}"
toolframe_args=()
while (($#)); do
  case "$1" in
    --bundle)
      toolframe_profile="${2:?--bundle requires standard or core}"
      shift 2 ;;
    --native-wheelhouse)
      toolframe_native_wheels="$(cd "${2:?--native-wheelhouse requires a directory}" && pwd)"
      shift 2 ;;
    --target)
      toolframe_target="${2:?--target requires a Rust target}"
      toolframe_args+=("$1" "$2")
      shift 2 ;;
    --target=*)
      toolframe_target="${1#--target=}"
      toolframe_args+=("$1")
      shift ;;
    --help|-h)
      echo "Usage: $0 [--bundle standard|core] [--target RUST_TARGET] [--native-wheelhouse PATH] [cargo build options]"
      echo "standard (default): core, SDK, Cordis, six providers and Entry Register."
      echo "core: only core, SDK and Cordis. First use may download Python/public dependencies."
      exit 0 ;;
    *) toolframe_args+=("$1"); shift ;;
  esac
done
case "$toolframe_profile" in
  standard|core) ;;
  *) echo "Unknown bundle: $toolframe_profile (choose standard or core)" >&2; exit 2 ;;
esac
toolframe_host="$(rustc -vV | sed -n 's/^host: //p')"
toolframe_platform="${toolframe_target:-$toolframe_host}"
toolframe_bundle="$toolframe_core/build/bundles/$toolframe_profile-$toolframe_platform"
mkdir -p "$toolframe_bundle"
# Do not accidentally embed obsolete versions or wheels from another profile.
for wheel in "$toolframe_bundle"/*.whl; do
  if [[ -f "$wheel" ]]; then rm "$wheel"; fi
done
toolframe_repositories=(toolframe-sdk fastapi-cordis toolframe-core)
if [[ "$toolframe_profile" == standard ]]; then
  toolframe_repositories+=(toolframe-storage-sqlite toolframe-streamlit-dashboard toolframe-fastmcp toolframe-llms-txt toolframe-demo-entry-register toolframe-auth toolframe-dashboard-auth)
  if [[ "$toolframe_platform" == x86_64-apple-darwin ]]; then
    toolframe_crypto="cryptography-50.0.1-cp314-abi3-macosx_12_0_x86_64.whl"
    if [[ ! -f "$toolframe_native_wheels/$toolframe_crypto" ]]; then
      echo "Intel standard bundle requires $toolframe_crypto in $toolframe_native_wheels." >&2
      echo "Build it with: uv run --project $toolframe_workspace/toolframe-fastmcp python $toolframe_workspace/toolframe-fastmcp/scripts/build-macos-intel.py --out-dir $toolframe_native_wheels" >&2
      exit 2
    fi
    cp "$toolframe_native_wheels/$toolframe_crypto" "$toolframe_bundle/"
  fi
fi
for repository in "${toolframe_repositories[@]}"; do
  uv build --no-sources --wheel --out-dir "$toolframe_bundle" "$toolframe_workspace/$repository"
done
cd "$toolframe_core"
TOOLFRAME_BUNDLE_DIR="$toolframe_bundle" cargo build --manifest-path "$toolframe_core/Cargo.toml" --release ${toolframe_args[@]+"${toolframe_args[@]}"}
toolframe_target_dir="${CARGO_TARGET_DIR:-$toolframe_core/target}"
toolframe_executable="$toolframe_target_dir${toolframe_target:+/$toolframe_target}/release/toolframe"
toolframe_version="$(sed -n 's/^version = "\([^"]*\)"/\1/p' "$toolframe_core/Cargo.toml" | head -n 1)"
mkdir -p "$toolframe_core/dist"
toolframe_release="$toolframe_core/dist/toolframe-$toolframe_version-$toolframe_profile-$toolframe_platform"
cp "$toolframe_executable" "$toolframe_release"
echo "Built $toolframe_release"
