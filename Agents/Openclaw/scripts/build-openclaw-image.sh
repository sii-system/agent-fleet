#!/usr/bin/env bash
# Build the openclaw Docker image.
#
# Default:       builds openclaw:local from the openclaw repo.
# OPIK_PLUGIN=enabled: builds openclaw:local-opik with the opik-tracer plugin.
#
# Variables:
#   OPENCLAW_REPO   OpenClaw git repo URL  (default: https://github.com/openclaw/openclaw.git)
#   TRACE_PLUGIN_SOURCE_DIR  agent-opik-plugin checkout (default: repo submodule)
#   NPM_CONFIG_REGISTRY  npm/pnpm registry mirror for builds
#   PIP_INDEX_URL        pip index mirror for the Opik image layer
#   PIP_EXTRA_INDEX_URL  optional extra pip index for the Opik image layer
#   PIP_TRUSTED_HOST     optional pip trusted host for the Opik image layer
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$PROJECT_DIR/../.." && pwd)"

# Match setup.sh's configuration precedence. Save the caller environment first
# so one-off overrides remain stronger than config files.
__caller_env="$(export -p)"
for config_file in \
  "$REPO_ROOT/config.env" \
  "$REPO_ROOT/config.local.env" \
  "$PROJECT_DIR/config/fleet.env"; do
  if [ -f "$config_file" ]; then
    # shellcheck source=/dev/null
    . "$config_file"
  fi
done
eval "$__caller_env"
unset __caller_env config_file

OPENCLAW_REPO="${OPENCLAW_REPO:-https://github.com/openclaw/openclaw.git}"

# Keep the top-level tracing switch authoritative over stale fleet/plugin
# configuration, matching setup.py and the benchmark launchers.
case "${TRACE_TO_OPIK:-true}" in
  false|0)
    if [ "${OPIK_PLUGIN:-}" = "enabled" ]; then
      echo "TRACE_TO_OPIK=false overrides OPIK_PLUGIN=enabled; tracing plugin disabled" >&2
    fi
    OPIK_PLUGIN=disabled
    ;;
esac

OPENCLAW_CACHE="$PROJECT_DIR/cache/openclaw"
OPENCLAW_SESSION_AFFINITY_PATCH="$PROJECT_DIR/patches/openclaw/openclaw-session-affinity.patch"
TRACE_PLUGIN_SOURCE_DIR="${TRACE_PLUGIN_SOURCE_DIR:-$REPO_ROOT/third_party/agent-opik-plugin}"
PLUGIN_SRC="$TRACE_PLUGIN_SOURCE_DIR/harness/openclaw"

OPIK_DOCKER_BUILD_ARGS=()

if [ -n "${NPM_CONFIG_REGISTRY:-}" ]; then
  export NPM_CONFIG_REGISTRY
fi

add_opik_build_arg() {
  local name="$1"
  local value="${!name:-}"
  if [ -n "$value" ]; then
    OPIK_DOCKER_BUILD_ARGS+=(--build-arg "$name=$value")
  fi
}

add_opik_build_arg PIP_INDEX_URL
add_opik_build_arg PIP_EXTRA_INDEX_URL
add_opik_build_arg PIP_TRUSTED_HOST
add_opik_build_arg NPM_CONFIG_REGISTRY

docker_build_load() {
  local tag="$1"
  shift
  if docker buildx version >/dev/null 2>&1; then
    docker buildx build --load -t "$tag" "$@"
  else
    docker build -t "$tag" "$@"
  fi
}

update_npmrc_mirror() {
  local repo_dir="$1"
  local npmrc="$repo_dir/.npmrc"
  local tmp="${npmrc}.tmp.$$"
  local had_npmrc=0

  if [ -f "$npmrc" ]; then
    had_npmrc=1
    awk '
      /^# agent-fleet mirror start$/ { skip = 1; next }
      /^# agent-fleet mirror end$/ { skip = 0; next }
      skip != 1 { print }
    ' "$npmrc" > "$tmp"
  else
    : > "$tmp"
  fi

  if [ -n "${NPM_CONFIG_REGISTRY:-}" ]; then
    {
      cat "$tmp"
      printf '\n# agent-fleet mirror start\n'
      printf 'registry=%s\n' "$NPM_CONFIG_REGISTRY"
      printf '# agent-fleet mirror end\n'
    } > "${tmp}.with-mirror"
    mv "${tmp}.with-mirror" "$tmp"
  fi

  if [ -s "$tmp" ] || [ -n "${NPM_CONFIG_REGISTRY:-}" ]; then
    mv "$tmp" "$npmrc"
  elif [ "$had_npmrc" -eq 1 ]; then
    mv "$tmp" "$npmrc"
  else
    rm -f "$tmp"
  fi
}

# ── Step 1: Clone or update the openclaw repo ──
OPENCLAW_PINNED_REF="v2026.5.10-beta.1"

if [ -d "$OPENCLAW_CACHE/.git" ]; then
  echo "Updating existing openclaw cache..."
  git -C "$OPENCLAW_CACHE" fetch --all || true
else
  echo "Cloning openclaw repo..."
  rm -rf "$OPENCLAW_CACHE"
  git clone "$OPENCLAW_REPO" "$OPENCLAW_CACHE"
fi

echo "Checking out pinned openclaw ref: $OPENCLAW_PINNED_REF"
git -C "$OPENCLAW_CACHE" checkout "$OPENCLAW_PINNED_REF"
git -C "$OPENCLAW_CACHE" reset --hard "$OPENCLAW_PINNED_REF"

update_npmrc_mirror "$OPENCLAW_CACHE"
if [ -f "$OPENCLAW_SESSION_AFFINITY_PATCH" ]; then
  echo "Applying OpenClaw session affinity patch..."
  git -C "$OPENCLAW_CACHE" apply --unidiff-zero "$OPENCLAW_SESSION_AFFINITY_PATCH"
fi

# ── Step 2: Build openclaw:local ──
echo "Building openclaw:local..."
docker_build_load openclaw:local "$OPENCLAW_CACHE"

echo ""
echo "Done. Image: openclaw:local"

# ── Opik layer (only when OPIK_PLUGIN=enabled) ──
if [ "${OPIK_PLUGIN:-}" != "enabled" ]; then
  exit 0
fi

echo ""
echo "OPIK_PLUGIN=enabled — building opik layer..."

# ── Step 3: Verify the agent-opik-plugin submodule ──
if [ ! -d "$PLUGIN_SRC" ] || [ ! -f "$TRACE_PLUGIN_SOURCE_DIR/src/sii_opik_plugin/openclaw/openclaw_opik_tracer.py" ]; then
  echo "Error: agent-opik-plugin submodule is missing or incomplete: $TRACE_PLUGIN_SOURCE_DIR" >&2
  echo "Run: git submodule update --init --recursive" >&2
  exit 1
fi

# ── Step 4: Build openclaw:local-opik ──
echo "Building openclaw:local-opik..."
OPIK_DOCKER_BUILD_ARGS+=(
  -f "$PROJECT_DIR/Dockerfile.opik"
  "$REPO_ROOT"
)
docker_build_load openclaw:local-opik \
  "${OPIK_DOCKER_BUILD_ARGS[@]}"

echo ""
echo "Done. Image: openclaw:local-opik"
echo "To use: OPIK_PLUGIN=enabled OPIK_URL=... OPIK_PROJECT_NAME=... ./Agents/Openclaw/scripts/setup.sh"
