#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=prerequisites.sh
source "$SCRIPT_DIR/prerequisites.sh"
agent_fleet_prerequisite_init_path
REPO_DIR="${REPO_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
# shellcheck source=fleet_spec_io.sh
source "$SCRIPT_DIR/fleet_spec_io.sh"
case "${1:-}" in
  -s|--spec) exec bash "$SCRIPT_DIR/fleet_spec.sh" "$@" ;;
  -p|--prompt) exec bash "$SCRIPT_DIR/fleet_prompt.sh" "$@" ;;
esac
for arg in "$@"; do
  if [[ "$arg" == "--prompt" || "$arg" == "-p" ]]; then
    printf '[ERROR] %s must be the first argument\n' "$arg" >&2
    exit 2
  elif [[ "$arg" == "--spec" || "$arg" == "-s" ]]; then
    exec bash "$SCRIPT_DIR/fleet_spec.sh" "$@"
  fi
done
usage() {
  cat <<EOF
Usage:
  $0 --taskset <taskset> [--agent <agent>] [--workers <n>] [--output <file>] [--detach] [--dry-run]
  $0 --spec <file|-> [file ...] [--output <file>] [--detach] [--dry-run]
  $0 --prompt <text> [--output <file>] [--detach] [--dry-run]

Short flags: -t --taskset, -a --agent, -n --workers, -s --spec, -p --prompt,
             -o --output, -d --detach

Tasksets: seta, smith, terminalbench21, sweverify, a registry id, a local
          path (./dir), or the OpenClaw tasksets: pinchbench, clawbio
Agents:   claude-code, opencode; openclaw for OpenClaw tasksets

Examples:
  $0 -t terminalbench21 -a claude-code -n 10 -d
  $0 -p "Run terminalbench21 with claude-code and 2 workers"
EOF
}

run_command() {
  if (( DRY_RUN )); then
    printf 'Command:'
    printf ' %q' "$@"
    printf '\n'
    exit 0
  fi
  exec "$@"
}

load_run_config() {
  local entry file name
  local -a caller_env=()

  # Normalize supported caller aliases before the snapshot so they also
  # override canonical values stored in config.local.env.
  if [[ -z "${BASE_URL:-}" && -n "${ANTHROPIC_BASE_URL:-}" ]]; then
    BASE_URL="$ANTHROPIC_BASE_URL"
    export BASE_URL
  fi
  if [[ -z "${API_KEY:-}" ]]; then
    if [[ -n "${AUTH_TOKEN:-}" ]]; then
      API_KEY="$AUTH_TOKEN"
    elif [[ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]]; then
      API_KEY="$ANTHROPIC_AUTH_TOKEN"
    fi
  fi
  if [[ -n "${API_KEY:-}" ]]; then
    export API_KEY
  fi
  if [[ -z "${MODEL:-}" && -n "${TB_MODEL:-}" ]]; then
    MODEL="$TB_MODEL"
    export MODEL
  fi
  while IFS= read -r -d '' entry; do
    caller_env+=("$entry")
  done < <(env -0)
  for file in "$REPO_DIR/config.env" "$REPO_DIR/config.local.env"; do
    if [[ -f "$file" ]]; then
      set -a
      # shellcheck source=/dev/null
      . "$file"
      set +a
    fi
  done
  for entry in "${caller_env[@]}"; do
    name="${entry%%=*}"
    [[ "$name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    export "$entry"
  done
}

validate_run_config() {
  local -a missing=()
  local required

  BASE_URL="${BASE_URL:-${ANTHROPIC_BASE_URL:-}}"
  API_KEY="${API_KEY:-${AUTH_TOKEN:-${ANTHROPIC_AUTH_TOKEN:-}}}"
  MODEL="${MODEL:-${TB_MODEL:-}}"
  export BASE_URL API_KEY MODEL
  for required in BASE_URL API_KEY MODEL; do
    [[ -n "${!required:-}" ]] || missing+=("$required")
  done
  case "${TRACE_TO_OPIK:-true}" in
    false|0) ;;
    *) [[ -n "${OPIK_URL:-}" ]] || missing+=("OPIK_URL (required when TRACE_TO_OPIK=true)") ;;
  esac
  if [[ ${#missing[@]} -gt 0 ]]; then
    printf '[ERROR] missing required configuration: %s\n' "${missing[*]}" >&2
    printf '[ERROR] run ./scripts/setup.sh or add the values to config.local.env\n' >&2
    return 1
  fi
}

apply_fleet_spec() {
  TASKSET="$(jq -r '.taskset' <<<"$FLEET_SPEC_JSON")"
  AGENT_ARG="$(jq -r 'if has("agent") then .agent else "" end' <<<"$FLEET_SPEC_JSON")"
  WORKERS="$(jq -r 'if has("workers") then (.workers | tostring) else "" end' <<<"$FLEET_SPEC_JSON")"
}

TASKSET="" AGENT_ARG="" WORKERS="" OUTPUT="" FLEET_SPEC_JSON=""
DETACH=0 DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -t|--taskset) TASKSET="$2"; shift 2 ;;
    -a|--agent) AGENT_ARG="$2"; shift 2 ;;
    -n|--workers) WORKERS="$2"; shift 2 ;;
    -o|--output)
      [[ $# -ge 2 && -n "$2" ]] || { printf '[ERROR] --output requires a non-empty file path\n' >&2; exit 2; }
      if fleet_spec_is_option_shaped "$2"; then
        printf '[ERROR] --output requires a file path; use ./%s for a file literally named %s\n' "$2" "$2" >&2
        exit 2
      fi
      OUTPUT="$2"; shift 2
      ;;
    -d|--detach) DETACH=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

fleet_spec_validate_output_path "$OUTPUT" || exit $?

[[ -n "$TASKSET" ]] || { usage >&2; exit 2; }
if (( ! DRY_RUN )); then
  load_run_config
  validate_run_config || exit 1
fi
if [[ -n "$OUTPUT" ]]; then
  if [[ -z "$FLEET_SPEC_JSON" ]]; then
    fleet_spec_from_taskset_args "$TASKSET" "$AGENT_ARG" "$WORKERS"
    apply_fleet_spec
  fi
  fleet_spec_write "$OUTPUT" "$FLEET_SPEC_JSON"
fi

REQUESTED_AGENT="${AGENT_ARG:-${AGENT:-}}"
if [[ "$TASKSET" == "pinchbench" || "$TASKSET" == "clawbio" ]] &&
   [[ -n "$REQUESTED_AGENT" && "$REQUESTED_AGENT" != "openclaw" ]]; then
  printf '[WARN] requested agent: %s; taskset: %s; actual agent: openclaw (requested agent ignored)\n' "$REQUESTED_AGENT" "$TASKSET" >&2
fi
if (( DETACH )) && [[ "$TASKSET" == "pinchbench" || "$TASKSET" == "clawbio" ]]; then
  printf '[WARN] --detach ignored for taskset: %s; runner remains in foreground\n' "$TASKSET" >&2
fi

case "$TASKSET" in
  pinchbench)
    cmd=(python3 "$REPO_DIR/Tasks/Pinchbench/scripts/run-parallel-workers.py")
    [[ -z "$WORKERS" ]] || cmd+=(--instances "$WORKERS")
    run_command "${cmd[@]}"
    ;;
  clawbio)
    cmd=(bash "$REPO_DIR/Tasks/clawBio/scripts/run-openclaw-clawbio.sh")
    [[ -z "$WORKERS" ]] || cmd=(env "COUNT=$WORKERS" "${cmd[@]}")
    run_command "${cmd[@]}"
    ;;
esac

harbor_env=()
case "$TASKSET" in
  /*|./*|../*|.|..|\~/*)
    taskset_path="${TASKSET/#\~/$HOME}"
    [[ "$taskset_path" == /* ]] || taskset_path="$PWD/$taskset_path"
    harbor_env+=("DATASET_NAME=auto" "DATASET_PATH=$taskset_path" "TB_PATH=$taskset_path")
    ;;
  *) harbor_env+=("DATASET_NAME=$TASKSET") ;;
esac

[[ -z "$AGENT_ARG" ]] || harbor_env+=("AGENT=$AGENT_ARG" "TB_AGENT=$AGENT_ARG")
[[ -z "$WORKERS" ]] || harbor_env+=("TOTAL_WORKERS=$WORKERS" "TB_N_CONCURRENT=$WORKERS")

# Assemble the full command in one always-non-empty array: expanding an
# empty array under `set -u` is an unbound-variable error on bash < 4.4
# (macOS /bin/bash 3.2), which broke every run without --detach.
harbor_cmd=(env "${harbor_env[@]}" bash "$REPO_DIR/Agents/utils/common/Harbor/start.sh")
(( DETACH )) && harbor_cmd+=(--detach)
run_command "${harbor_cmd[@]}"
