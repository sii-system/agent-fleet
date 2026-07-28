#!/usr/bin/env bash
set -euo pipefail

# Unified launcher for ClawBio benchmark runs.
# Responsibilities:
# 1) prepare cache and fleet config paths
# 2) setup and start OpenClaw fleet
# 3) run benchmark iterations via run-benchmark.py
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BENCH_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$BENCH_DIR/../.." && pwd)"
OPENCLAW_DIR="$REPO_ROOT/Agents/Openclaw"

TIMESTAMP="${TIMESTAMP:-$(date +%Y%m%d-%H%M%S)}"
COUNT="${COUNT:-}"
ITERATIONS="${ITERATIONS:-1}"
RUN_ROOT="${RUN_ROOT:-$BENCH_DIR/runs/$TIMESTAMP}"
TASK_CONFIG="${TASK_CONFIG:-$BENCH_DIR/config/tasks.json}"
SELECTED_TASKS=""
VALIDATE_TASKS_ONLY=0

# Keep model/provider config sourced from config.env or caller env.
BASE_URL="${BASE_URL:-}"
API_KEY="${API_KEY:-}"
MODEL="${MODEL:-}"

OPENCLAW_UID="${OPENCLAW_UID:-$(id -u)}"
OPENCLAW_GID="${OPENCLAW_GID:-$(id -g)}"
OPENCLAW_CONTAINER_USER="${OPENCLAW_CONTAINER_USER:-$OPENCLAW_UID}"

# Default is resolved after config loading so it can follow TRACE_TO_OPIK.
OPIK_PLUGIN="${OPIK_PLUGIN:-}"
OPIK_URL="${OPIK_URL:-}"
OPIK_WORKSPACE="${OPIK_WORKSPACE:-default}"
OPIK_API_KEY="${OPIK_API_KEY:-}"
project_inst_tag="${COUNT:-auto}"
OPIK_PROJECT_NAME="${OPIK_PROJECT_NAME:-openclaw-clawbio-${TIMESTAMP}-inst${project_inst_tag}-iter${ITERATIONS}}"

OPENCLAW_IMAGE_POLICY="${OPENCLAW_IMAGE_POLICY:-if-missing}"
CONFIG_BASE="${CONFIG_BASE:-$RUN_ROOT/fleet/openclaw-config}"
WORKSPACE_BASE="${WORKSPACE_BASE:-$RUN_ROOT/fleet/openclaw-workspaces}"
PLUGIN_CACHE_DIR="${PLUGIN_CACHE_DIR:-$BENCH_DIR/cache}"

usage() {
  cat <<EOF
Usage: $(basename "$0") [--tasks <id>[,id...]]

One-command launcher for ClawBio benchmark:
1) optionally build/reuse OpenClaw image
2) setup OpenClaw fleet with optional Opik tracing
3) patch clawbio plugin config
4) start fleet containers
5) run benchmark via run-benchmark.py (native -n iterations)

Optional env vars:
  COUNT, ITERATIONS, TASK_CONFIG, RUN_ROOT
  TRACE_TO_OPIK, OPIK_URL, OPIK_WORKSPACE, OPIK_API_KEY, OPIK_PROJECT_NAME
  OPENCLAW_IMAGE_POLICY=if-missing|always
  CONFIG_BASE, WORKSPACE_BASE, PLUGIN_CACHE_DIR

Provider/fleet vars are read from environment or the repo-root config.env:
  BASE_URL, API_KEY, MODEL

Required ClawBio benchmark security settings:
  Loaded from Tasks/clawBio/config/benchmark.env:
  SANDBOX_MODE=off, EXEC_SECURITY=full, EXEC_ASK=off
  WORKSPACE_ONLY=false
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tasks)
      [[ $# -ge 2 && -n "$2" ]] || {
        echo "Error: --tasks requires a non-empty value." >&2
        exit 2
      }
      SELECTED_TASKS="$2"
      shift 2
      ;;
    --validate-tasks-only)
      VALIDATE_TASKS_ONLY=1
      shift
      ;;
    -h|--help) usage; exit 0 ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
done
if (( VALIDATE_TASKS_ONLY )) && [[ -z "$SELECTED_TASKS" ]]; then
  echo "Error: --validate-tasks-only requires --tasks." >&2
  exit 2
fi
readonly CLI_SELECTED_TASKS="$SELECTED_TASKS"

# Load shared site config (config.env), private overrides/secrets
# (config.local.env, git-ignored), OpenClaw fleet defaults, and finally the
# committed ClawBio benchmark profile. Caller-provided env wins over all config
# files, so snapshot it now and re-apply after sourcing.
__caller_env="$(export -p)"
root_cfg="$REPO_ROOT/config.env"
if [[ -f "$root_cfg" ]]; then
  set -a
  # shellcheck source=/dev/null
  . "$root_cfg"
  set +a
fi
local_cfg="$REPO_ROOT/config.local.env"
if [[ -f "$local_cfg" ]]; then
  set -a
  # shellcheck source=/dev/null
  . "$local_cfg"
  set +a
fi
fleet_env="$OPENCLAW_DIR/config/fleet.env"
if [[ -f "$fleet_env" ]]; then
  set -a
  # shellcheck source=/dev/null
  . "$fleet_env"
  set +a
fi
clawbio_profile="$BENCH_DIR/config/benchmark.env"
if [[ -f "$clawbio_profile" ]]; then
  set -a
  # shellcheck source=/dev/null
  . "$clawbio_profile"
  set +a
fi
# Caller-provided env wins over all the config files above.
eval "$__caller_env"
unset __caller_env
SELECTED_TASKS="$CLI_SELECTED_TASKS"

if [[ -n "$SELECTED_TASKS" ]]; then
  python3 "$BENCH_DIR/scripts/run-benchmark.py" \
    --config "$TASK_CONFIG" \
    --tasks "$SELECTED_TASKS" \
    --validate-tasks-only
fi
(( VALIDATE_TASKS_ONLY == 0 )) || exit 0

if [[ ! -f "$clawbio_profile" ]]; then
  echo "Error: missing ClawBio benchmark profile: $clawbio_profile" >&2
  exit 2
fi

# ClawBio loads its skill outside the instance workspace, executes unattended
# shell/Python/R commands, and writes benchmark artifacts. Resolve the same
# defaults as setup.py, then reject incompatible settings before creating run
# directories, building images, preparing caches, or changing the fleet.
SANDBOX_MODE="${SANDBOX_MODE:-off}"
EXEC_SECURITY="${EXEC_SECURITY:-deny}"
EXEC_ASK="${EXEC_ASK:-always}"
WORKSPACE_ONLY="${WORKSPACE_ONLY:-true}"
DOCKER_COMPOSE_READ_ONLY="${DOCKER_COMPOSE_READ_ONLY:-true}"

security_mismatches=()
[[ "$SANDBOX_MODE" == "off" ]] ||
  security_mismatches+=("SANDBOX_MODE")
[[ "$EXEC_SECURITY" == "full" ]] ||
  security_mismatches+=("EXEC_SECURITY")
[[ "$EXEC_ASK" == "off" ]] ||
  security_mismatches+=("EXEC_ASK")
[[ "$WORKSPACE_ONLY" == "false" ]] ||
  security_mismatches+=("WORKSPACE_ONLY")

if (( ${#security_mismatches[@]} > 0 )); then
  echo "Error: ClawBio benchmark security preflight failed before fleet setup." >&2
  echo "ClawBio loads its skill outside the workspace and executes unattended commands." >&2
  echo "Incompatible settings:" >&2
  for setting in "${security_mismatches[@]}"; do
    case "$setting" in
      SANDBOX_MODE) required="off" ;;
      EXEC_SECURITY) required="full" ;;
      EXEC_ASK) required="off" ;;
      WORKSPACE_ONLY) required="false" ;;
    esac
    printf '  %s=%q (required: %q)\n' "$setting" "${!setting}" "$required" >&2
  done
  cat >&2 <<EOF
The dedicated profile is defined in Tasks/clawBio/config/benchmark.env.
Remove conflicting runtime overrides or restore these profile values:
  SANDBOX_MODE=off EXEC_SECURITY=full EXEC_ASK=off WORKSPACE_ONLY=false
See Tasks/clawBio/README.md#clawbio-benchmark-security.
The general OpenClaw security defaults were not changed.
EOF
  exit 2
fi

cat >&2 <<EOF
Warning: applying the permissive ClawBio benchmark profile from
  Tasks/clawBio/config/benchmark.env
  SANDBOX_MODE=off EXEC_SECURITY=full EXEC_ASK=off WORKSPACE_ONLY=false
Use it only for the generated ClawBio fleet. The profile remains active until
that fleet is stopped or regenerated.
EOF

# TRACE_TO_OPIK is the authoritative switch documented in the root README:
# tracing off forces the plugin off, even over an explicit
# OPIK_PLUGIN=enabled lingering in fleet.env or another config file. With
# tracing on, an explicit OPIK_PLUGIN still wins and the default is enabled.
case "${TRACE_TO_OPIK:-true}" in
  false|0)
    if [[ "$OPIK_PLUGIN" == "enabled" ]]; then
      echo "TRACE_TO_OPIK=false overrides OPIK_PLUGIN=enabled; tracing plugin disabled" >&2
    fi
    OPIK_PLUGIN=disabled
    # These values may have been exported while loading config files. Clear
    # them before invoking setup/build/benchmark children so trace-off runs do
    # not retain unnecessary Opik credentials in their process environments.
    OPIK_URL=""
    OPIK_WORKSPACE=""
    OPIK_API_KEY=""
    OPIK_PROJECT_NAME=""
    ;;
  *) OPIK_PLUGIN="${OPIK_PLUGIN:-enabled}" ;;
esac

if [[ "$OPIK_PLUGIN" == "enabled" ]]; then
  if [[ -z "$OPIK_URL" ]]; then
    echo "Error: OPIK_PLUGIN=enabled requires OPIK_URL." >&2
    exit 1
  fi
fi

image_exists() {
  docker image inspect "$1" >/dev/null 2>&1
}

mkdir -p "$RUN_ROOT" "$CONFIG_BASE" "$WORKSPACE_BASE"

echo "== OpenClaw ClawBio launcher =="
echo "timestamp:      $TIMESTAMP"
echo "instances:      ${COUNT:-<from fleet env/setup default>}"
echo "iterations:     $ITERATIONS"
echo "run root:       $RUN_ROOT"
echo "task config:    $TASK_CONFIG"
echo "image policy:   $OPENCLAW_IMAGE_POLICY"
if [[ "$OPIK_PLUGIN" == "enabled" ]]; then
  echo "opik tracing:   enabled"
  echo "opik project:   $OPIK_PROJECT_NAME"
  echo "opik url:       $OPIK_URL"
else
  echo "opik tracing:   disabled"
fi
echo

cd "$REPO_ROOT"

need_build=0
if [[ "$OPENCLAW_IMAGE_POLICY" == "always" ]]; then
  need_build=1
elif ! image_exists "openclaw:local"; then
  need_build=1
elif [[ "$OPIK_PLUGIN" == "enabled" ]] && ! image_exists "openclaw:local-opik"; then
  need_build=1
fi

if [[ "$need_build" -eq 1 ]]; then
  TRACE_TO_OPIK="${TRACE_TO_OPIK:-true}" \
    OPIK_PLUGIN="$OPIK_PLUGIN" \
    "$OPENCLAW_DIR/scripts/build-openclaw-image.sh"
elif [[ "$OPIK_PLUGIN" == "enabled" ]]; then
  echo "Reusing local images: openclaw:local and openclaw:local-opik"
else
  echo "Reusing local image: openclaw:local"
fi

"$BENCH_DIR/scripts/prewarm-cache.sh" --cache-dir "$PLUGIN_CACHE_DIR"

env_args=(
  "TRACE_TO_OPIK=${TRACE_TO_OPIK:-true}"
  "OPIK_PLUGIN=$OPIK_PLUGIN"
  "OPENCLAW_UID=$OPENCLAW_UID"
  "OPENCLAW_GID=$OPENCLAW_GID"
  "OPENCLAW_CONTAINER_USER=$OPENCLAW_CONTAINER_USER"
  "CONFIG_BASE=$CONFIG_BASE"
  "WORKSPACE_BASE=$WORKSPACE_BASE"
  "PLUGIN_CACHE_DIR=$PLUGIN_CACHE_DIR"
)

if [[ "$OPIK_PLUGIN" == "enabled" ]]; then
  env_args+=(
    "OPIK_URL=$OPIK_URL"
    "OPIK_WORKSPACE=$OPIK_WORKSPACE"
    "OPIK_API_KEY=$OPIK_API_KEY"
    "OPIK_PROJECT_NAME=$OPIK_PROJECT_NAME"
  )
fi

if [[ -n "$BASE_URL" ]]; then env_args+=("BASE_URL=$BASE_URL"); fi
if [[ -n "$API_KEY" ]]; then env_args+=("API_KEY=$API_KEY"); fi
if [[ -n "$MODEL" ]]; then env_args+=("MODEL=$MODEL"); fi
if [[ -n "$COUNT" ]]; then env_args+=("COUNT=$COUNT"); fi
env_args+=(
  "SANDBOX_MODE=$SANDBOX_MODE"
  "EXEC_SECURITY=$EXEC_SECURITY"
  "EXEC_ASK=$EXEC_ASK"
  "WORKSPACE_ONLY=$WORKSPACE_ONLY"
  "DOCKER_COMPOSE_READ_ONLY=$DOCKER_COMPOSE_READ_ONLY"
)

if [[ -n "$COUNT" ]]; then
  env "${env_args[@]}" "$OPENCLAW_DIR/scripts/setup.sh" "$COUNT"
else
  env "${env_args[@]}" "$OPENCLAW_DIR/scripts/setup.sh"
fi
"$BENCH_DIR/scripts/patch-plugin-config.sh" --config-base "$CONFIG_BASE"
docker compose -f "$OPENCLAW_DIR/docker-compose.yml" down
docker compose -f "$OPENCLAW_DIR/docker-compose.yml" up -d
"$OPENCLAW_DIR/scripts/openclaw-fleet.sh" status

run_cmd=("$BENCH_DIR/scripts/run-benchmark.py" --config "$TASK_CONFIG" --output-dir "$(dirname "$RUN_ROOT")" -n "$ITERATIONS" --run-id "$(basename "$RUN_ROOT")")
if [[ -n "$SELECTED_TASKS" ]]; then
  run_cmd+=(--tasks "$SELECTED_TASKS")
fi
if [[ -n "$COUNT" ]]; then
  run_cmd+=(--instances "$COUNT")
fi
"${run_cmd[@]}"

# Create 'latest' symlink so the most recent run is easy to find.
RUNS_DIR="$(dirname "$RUN_ROOT")"
RUN_NAME="$(basename "$RUN_ROOT")"
LATEST_LINK="$RUNS_DIR/latest"
if [[ -e "$LATEST_LINK" || -L "$LATEST_LINK" ]]; then
  rm -f "$LATEST_LINK"
fi
ln -s "$RUN_NAME" "$LATEST_LINK"

echo
echo "Run complete."
if [[ "$OPIK_PLUGIN" == "enabled" ]]; then
  echo "Opik project: $OPIK_PROJECT_NAME"
else
  echo "Opik tracing: disabled"
fi
echo "Run root:      $RUN_ROOT"
echo "Latest link:   $LATEST_LINK -> $RUN_NAME"
