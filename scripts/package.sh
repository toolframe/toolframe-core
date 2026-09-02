#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
UV_SOURCE="${UV_BIN:-$(command -v uv || true)}"
DESTINATION="$REPOSITORY_ROOT/dist/lite-mcp"
SKILL_SOURCE="$REPOSITORY_ROOT/skills/lite-mcp-business-app"
LLMS_FILE_SOURCE="$REPOSITORY_ROOT/llms.txt"
LLMS_DIRECTORY_SOURCE="$REPOSITORY_ROOT/llms"
STAGING_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/lite-mcp-package.XXXXXX")"

cleanup() {
  rm -rf "$STAGING_ROOT"
}
trap cleanup EXIT

if [[ -z "$UV_SOURCE" || ! -x "$UV_SOURCE" ]]; then
  echo "error: set UV_BIN to an executable uv binary or install uv on PATH" >&2
  exit 1
fi

if [[ -e "$DESTINATION" || -L "$DESTINATION" ]]; then
  echo "error: $DESTINATION already exists; move it aside before packaging so user data is never overwritten" >&2
  exit 1
fi

UV_VERSION="$($UV_SOURCE --version | awk '{print $2}')"
IFS=. read -r UV_MAJOR UV_MINOR UV_PATCH_REST <<<"$UV_VERSION"
UV_PATCH="${UV_PATCH_REST%%[^0-9]*}"
if (( UV_MAJOR == 0 && (UV_MINOR < 12 || (UV_MINOR == 12 && UV_PATCH < 8)) )); then
  echo "error: uv 0.12.8 or newer is required to provision final Python 3.14 (found $UV_VERSION)" >&2
  exit 1
fi

if [[ ! -d "$SKILL_SOURCE" || -L "$SKILL_SOURCE" ]]; then
  echo "error: business-app skill must be a regular directory at $SKILL_SOURCE" >&2
  exit 1
fi
if [[ ! -f "$SKILL_SOURCE/SKILL.md" || -L "$SKILL_SOURCE/SKILL.md" ]]; then
  echo "error: business-app skill SKILL.md must be a regular non-symlink file at $SKILL_SOURCE/SKILL.md" >&2
  exit 1
fi
SKILL_SYMLINK="$(find -P "$SKILL_SOURCE" -type l -print -quit)"
if [[ -n "$SKILL_SYMLINK" ]]; then
  echo "error: business-app skill contains a symlink: $SKILL_SYMLINK" >&2
  exit 1
fi
if [[ ! -f "$REPOSITORY_ROOT/examples/idempotent_create_entry.py" || -L "$REPOSITORY_ROOT/examples/idempotent_create_entry.py" ]]; then
  echo "error: idempotency reference must be a regular non-symlink file at $REPOSITORY_ROOT/examples/idempotent_create_entry.py" >&2
  exit 1
fi
if [[ ! -f "$LLMS_FILE_SOURCE" || -L "$LLMS_FILE_SOURCE" ]]; then
  echo "error: llms.txt must be a regular non-symlink file at $LLMS_FILE_SOURCE" >&2
  exit 1
fi
if [[ ! -d "$LLMS_DIRECTORY_SOURCE" || -L "$LLMS_DIRECTORY_SOURCE" ]]; then
  echo "error: llms detail source must be a regular directory at $LLMS_DIRECTORY_SOURCE" >&2
  exit 1
fi
LLMS_INVALID_ENTRY="$(find -P "$LLMS_DIRECTORY_SOURCE" -mindepth 1 -maxdepth 1 ! -type f -print -quit)"
if [[ -n "$LLMS_INVALID_ENTRY" ]]; then
  echo "error: llms detail source must contain direct regular files only: $LLMS_INVALID_ENTRY" >&2
  exit 1
fi
LLMS_DETAIL_COUNT=0
while IFS= read -r -d '' llms_source; do
  llms_name="$(basename "$llms_source")"
  if [[ ! "$llms_name" =~ ^[a-z0-9][a-z0-9_-]*\.md$ ]]; then
    echo "error: llms detail filename must be lowercase and end in .md: $llms_source" >&2
    exit 1
  fi
  LLMS_DETAIL_COUNT=$((LLMS_DETAIL_COUNT + 1))
done < <(find -P "$LLMS_DIRECTORY_SOURCE" -mindepth 1 -maxdepth 1 -type f -print0)
if (( LLMS_DETAIL_COUNT == 0 )); then
  echo "error: llms detail source must contain at least one Markdown file" >&2
  exit 1
fi

cd "$REPOSITORY_ROOT"
cargo build --release

PACKAGE="$STAGING_ROOT/lite-mcp"
if [[ ! -d "$REPOSITORY_ROOT/dashboard" ]]; then
  echo "error: dashboard scaffold is missing from $REPOSITORY_ROOT/dashboard" >&2
  exit 1
fi
if [[ ! -d "$REPOSITORY_ROOT/migrations" || -L "$REPOSITORY_ROOT/migrations" ]]; then
  echo "error: migration scaffold must be a regular directory at $REPOSITORY_ROOT/migrations" >&2
  exit 1
fi
if [[ ! -f "$REPOSITORY_ROOT/migrations/0000_bootstrap.sql" || -L "$REPOSITORY_ROOT/migrations/0000_bootstrap.sql" ]]; then
  echo "error: migration bootstrap is missing from $REPOSITORY_ROOT/migrations" >&2
  exit 1
fi
if [[ ! -f "$REPOSITORY_ROOT/migrations/README.md" || -L "$REPOSITORY_ROOT/migrations/README.md" ]]; then
  echo "error: migration authoring reference is missing from $REPOSITORY_ROOT/migrations" >&2
  exit 1
fi
for migration_source in "$REPOSITORY_ROOT"/migrations/*.sql; do
  if [[ -L "$migration_source" || ! -f "$migration_source" ]]; then
    echo "error: migration source must be a regular non-symlink file: $migration_source" >&2
    exit 1
  fi
done

mkdir -p \
  "$PACKAGE/tools" \
  "$PACKAGE/dashboard" \
  "$PACKAGE/examples" \
  "$PACKAGE/llms" \
  "$PACKAGE/migrations" \
  "$PACKAGE/skills/lite-mcp-business-app" \
  "$PACKAGE/workspace" \
  "$PACKAGE/runtime" \
  "$PACKAGE/logs"
install -m 0755 "$REPOSITORY_ROOT/target/release/lite-mcp" "$PACKAGE/lite-mcp"
install -m 0755 "$UV_SOURCE" "$PACKAGE/uv"
install -m 0644 "$REPOSITORY_ROOT/tools/execute_sql.py" "$PACKAGE/tools/execute_sql.py"
install -m 0644 "$REPOSITORY_ROOT/tools/_artifact_example.py" "$PACKAGE/tools/_artifact_example.py"
install -m 0644 "$REPOSITORY_ROOT/examples/idempotent_create_entry.py" "$PACKAGE/examples/idempotent_create_entry.py"
install -m 0644 "$LLMS_FILE_SOURCE" "$PACKAGE/llms.txt"
install -m 0644 "$REPOSITORY_ROOT/README.md" "$PACKAGE/README.md"

# The LAN agent guide is editable application source. Keep the root index and
# its direct Markdown details together without following links or packaging
# unrelated files.
while IFS= read -r -d '' llms_source; do
  install -m 0644 "$llms_source" "$PACKAGE/llms/$(basename "$llms_source")"
done < <(find -P "$LLMS_DIRECTORY_SOURCE" -mindepth 1 -maxdepth 1 -type f -print0)

# The dashboard is intentionally editable in the one-folder distribution. Copy
# regular source files while excluding interpreter caches; never mutate the
# source tree and never package stale generated bytecode.
(
  cd "$REPOSITORY_ROOT/dashboard"
  find . -type d -name '__pycache__' -prune -o \
    -type f ! -name '*.pyc' ! -name '*.pyo' -print0 |
    while IFS= read -r -d '' relative_path; do
      destination_path="$PACKAGE/dashboard/$relative_path"
      mkdir -p "$(dirname "$destination_path")"
      cp -p "$relative_path" "$destination_path"
    done
)

# The business-app skill is a coding-agent handbook shipped beside the binary.
# Copy its complete regular-file source while excluding interpreter caches and
# bytecode; symlinks anywhere in the source were rejected before the build.
(
  cd "$SKILL_SOURCE"
  find -P . -type d -name '__pycache__' -prune -o -type d -print0 |
    while IFS= read -r -d '' relative_path; do
      destination_path="$PACKAGE/skills/lite-mcp-business-app/$relative_path"
      mkdir -p "$destination_path"
    done
  find -P . -type d -name '__pycache__' -prune -o \
    -type f ! -name '*.pyc' ! -name '*.pyo' -print0 |
    while IFS= read -r -d '' relative_path; do
      destination_path="$PACKAGE/skills/lite-mcp-business-app/$relative_path"
      mkdir -p "$(dirname "$destination_path")"
      cp -p "$relative_path" "$destination_path"
    done
)

# Migration files are user-owned schema history. Package every regular source
# file, preserve applied migration bytes, and never follow symlinks.
(
  cd "$REPOSITORY_ROOT/migrations"
  find . -type f -print0 |
    while IFS= read -r -d '' relative_path; do
      destination_path="$PACKAGE/migrations/$relative_path"
      mkdir -p "$(dirname "$destination_path")"
      cp -p "$relative_path" "$destination_path"
    done
)

mkdir -p "$REPOSITORY_ROOT/dist"
mv "$PACKAGE" "$DESTINATION"

echo "Packaged $DESTINATION"
echo "Run: $DESTINATION/lite-mcp"
