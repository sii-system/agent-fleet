#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=prerequisites.sh
source "$SCRIPT_DIR/prerequisites.sh"
agent_fleet_prerequisite_init_path
REPO_DIR="${REPO_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
# shellcheck source=config_loader.sh
source "$SCRIPT_DIR/config_loader.sh"
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
  $0 --taskset <taskset> [--task <name>[,name...]] [--agent <agent>] [--workers <n>] [--run-id <id>] [--output <file>] [--detach] [--dry-run]
  $0 --spec <file|-> [file ...] [--output <file>] [--detach] [--dry-run]
  $0 --prompt <text> [--output <file>] [--detach] [--dry-run]

Short flags: -t --taskset, -a --agent, -n --workers, -s --spec, -p --prompt,
             -o --output, -d --detach; --task has no short form

Each direct invocation starts a new run by default. Use --run-id to resume or
reset a named run; inherited RUN_ID and run-state paths are ignored.

Tasksets: seta, smith, terminalbench21, sweverify, browsecomp-plus, a registry
          id, a local path (./dir), or the OpenClaw tasksets: pinchbench, clawbio
Agents:   claude-code, opencode, pi; openclaw for OpenClaw tasksets
Use --task=<name> when a task ID begins with a dash.

Examples:
  $0 -t terminalbench21 --task fix-git -a claude-code -n 1
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
  agent_fleet_load_config "$REPO_DIR"
  agent_fleet_apply_auth_token_fallback
}

validate_run_config() {
  local -a missing=()
  local required

  for required in BASE_URL API_KEY MODEL; do
    [[ -n "${!required:-}" ]] || missing+=("$required")
  done
  # OPIK_URL is intentionally not required: an empty value means run without
  # Opik tracing.
  if [[ ${#missing[@]} -gt 0 ]]; then
    printf '[ERROR] missing required configuration: %s\n' "${missing[*]}" >&2
    printf '[ERROR] run ./scripts/setup.sh or add the values to config.local.env\n' >&2
    return 1
  fi
}

apply_fleet_spec() {
  TASKSET="$(jq -r '.taskset' <<<"$FLEET_SPEC_JSON")"
  FLEET_TASK="$(jq -r 'if has("task") then .task else "" end' <<<"$FLEET_SPEC_JSON")"
  AGENT_ARG="$(jq -r 'if has("agent") then .agent else "" end' <<<"$FLEET_SPEC_JSON")"
  WORKERS="$(jq -r 'if has("workers") then (.workers | tostring) else "" end' <<<"$FLEET_SPEC_JSON")"
}

TASKSET="" FLEET_TASK="" AGENT_ARG="" WORKERS="" RUN_ID_ARG="" OUTPUT="" FLEET_SPEC_JSON=""
TASK_VALUES=()
DETACH=0 DRY_RUN=0 VALIDATE_TASK_SELECTION=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -t|--taskset)
      [[ $# -ge 2 ]] || { printf '[ERROR] %s requires a value\n' "$1" >&2; exit 2; }
      TASKSET="$2"; shift 2
      ;;
    --task)
      [[ $# -ge 2 && -n "$2" ]] || { printf '[ERROR] --task requires a non-empty value\n' >&2; exit 2; }
      if fleet_spec_is_option_shaped "$2"; then
        printf '[ERROR] --task requires a task name, got option: %s\n' "$2" >&2
        exit 2
      fi
      TASK_VALUES[${#TASK_VALUES[@]}]="$2"; shift 2
      ;;
    --task=*)
      task_value="${1#*=}"
      [[ -n "$task_value" ]] || { printf '[ERROR] --task requires a non-empty value\n' >&2; exit 2; }
      TASK_VALUES[${#TASK_VALUES[@]}]="$task_value"; shift
      ;;
    -a|--agent)
      [[ $# -ge 2 ]] || { printf '[ERROR] %s requires a value\n' "$1" >&2; exit 2; }
      AGENT_ARG="$2"; shift 2
      ;;
    -n|--workers)
      [[ $# -ge 2 ]] || { printf '[ERROR] %s requires a value\n' "$1" >&2; exit 2; }
      WORKERS="$2"; shift 2
      ;;
    --run-id)
      [[ $# -ge 2 && -n "$2" ]] || { printf '[ERROR] --run-id requires a non-empty value\n' >&2; exit 2; }
      if fleet_spec_is_option_shaped "$2"; then
        printf '[ERROR] --run-id requires an id, got option: %s\n' "$2" >&2
        exit 2
      fi
      RUN_ID_ARG="$2"; shift 2
      ;;
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
    --validate-task-selection) VALIDATE_TASK_SELECTION=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

fleet_spec_validate_output_path "$OUTPUT" || exit $?
if [[ -n "$RUN_ID_ARG" && ! "$RUN_ID_ARG" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
  printf '[ERROR] --run-id must contain only letters, digits, dots, underscores, and hyphens\n' >&2
  exit 2
fi

if [[ ${#TASK_VALUES[@]} -gt 0 ]]; then
  fleet_spec_normalize_task_values "${TASK_VALUES[@]}" || exit $?
fi
if [[ -n "$FLEET_TASK" && -z "$TASKSET" ]]; then
  printf '[ERROR] --task requires --taskset; taskset inference is not supported\n' >&2
  exit 2
fi
[[ -n "$TASKSET" ]] || { usage >&2; exit 2; }
if [[ -n "$FLEET_TASK" ]]; then
  case "$TASKSET" in
    seta|smith|terminalbench21|sweverify|browsecomp|browsecomp-plus|pinchbench|clawbio|/*|./*|../*|.|..|\~/*) ;;
    *)
      printf '[ERROR] --task is unsupported for Harbor registry taskset: %s\n' "$TASKSET" >&2
      exit 2
      ;;
  esac
fi
if [[ -n "$FLEET_TASK" && "${ROLLOUT:-0}" == "1" ]]; then
  printf '[ERROR] --task is unsupported when ROLLOUT=1\n' >&2
  exit 2
fi
if (( ! DRY_RUN && ! VALIDATE_TASK_SELECTION )); then
  load_run_config
  if [[ -n "$FLEET_TASK" && "${ROLLOUT:-0}" == "1" ]]; then
    printf '[ERROR] --task is unsupported when ROLLOUT=1\n' >&2
    exit 2
  fi
  validate_run_config || exit 1
fi

# A direct CLI invocation owns a fresh run unless the run identity is visible
# in that invocation. Do not let an exported or saved RUN_ID, or one of its
# derived paths, silently turn a normal launch into a resume. Fleet Batch is
# the sole internal exception: it assigns unique child RUN_IDs and clears
# these paths before invoking this router.
if [[ -z "${FLEET_BATCH_HARBOR_RUNS:-}" ]]; then
  if [[ -n "$RUN_ID_ARG" ]]; then
    RUN_ID="$RUN_ID_ARG"
  else
    RUN_ID="fleet-direct-$(date -u '+%Y%m%d-%H%M%S')-$$"
  fi
  export RUN_ID
  unset OUTPUT_PATH TASK_FILE QUEUE_DIR RUNTIME_DIR LAYOUT_FILE JOBS_ROOT
  unset HARBOR_ONLINE_ANALYSIS_DIR HARBOR_ONLINE_ANALYSIS_PID_FILE HARBOR_ONLINE_ANALYSIS_LOG_FILE
  unset HARBOR_MONITOR_DIR HARBOR_MONITOR_PID_FILE HARBOR_MONITOR_LOG_FILE
  unset HARBOR_BENCHMARK_PID_FILE HARBOR_BENCHMARK_EXIT_FILE HARBOR_JOB_DIR_FILE
  unset RL_TRACE_LOG RL_SERVER_LOG RL_SERVER_PID_FILE RL_QUEUE_DIR RL_ACTIVE_DIR
  unset RL_JOB_QUEUE_ROOT RL_JOB_RUNTIME_ROOT
fi

if [[ -n "$OUTPUT" ]]; then
  if [[ -z "$FLEET_SPEC_JSON" ]]; then
    fleet_spec_from_taskset_args "$TASKSET" "$FLEET_TASK" "$AGENT_ARG" "$WORKERS"
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
if (( VALIDATE_TASK_SELECTION )) && [[ -z "$FLEET_TASK" ]]; then
  exit 0
fi

case "$TASKSET" in
  browsecomp|browsecomp-plus)
    cmd=(bash "$REPO_DIR/Tasks/BrowseComp-Plus/scripts/run.sh")
    [[ -z "$FLEET_TASK" ]] || cmd+=(--task "$FLEET_TASK")
    [[ -z "$AGENT_ARG" ]] || cmd+=(--agent "$AGENT_ARG")
    [[ -z "$WORKERS" ]] || cmd+=(--workers "$WORKERS")
    (( VALIDATE_TASK_SELECTION == 0 )) || cmd+=(--validate-tasks-only)
    (( DETACH == 0 )) || cmd+=(--detach)
    run_command "${cmd[@]}"
    ;;
  pinchbench)
    pinchbench_exact_task_selection=0
    [[ -z "$FLEET_TASK" ]] || pinchbench_exact_task_selection=1
    cmd=(
      env "PINCHBENCH_EXACT_TASK_SELECTION=$pinchbench_exact_task_selection"
      python3 "$REPO_DIR/Tasks/Pinchbench/scripts/run-parallel-workers.py"
    )
    [[ -z "$FLEET_TASK" ]] || cmd+=(--suite "$FLEET_TASK")
    [[ -z "$WORKERS" ]] || cmd+=(--instances "$WORKERS")
    (( VALIDATE_TASK_SELECTION == 0 )) || cmd+=(--validate-tasks-only)
    run_command "${cmd[@]}"
    ;;
  clawbio)
    cmd=(bash "$REPO_DIR/Tasks/clawBio/scripts/run-openclaw-clawbio.sh")
    [[ -z "$FLEET_TASK" ]] || cmd+=(--tasks "$FLEET_TASK")
    (( VALIDATE_TASK_SELECTION == 0 )) || cmd+=(--validate-tasks-only)
    [[ -z "$WORKERS" ]] || cmd=(env "COUNT=$WORKERS" "${cmd[@]}")
    run_command "${cmd[@]}"
    ;;
esac

harbor_env=()
case "$TASKSET" in
  /*|./*|../*|.|..|\~/*)
    taskset_path="${TASKSET/#\~/$HOME}"
    [[ "$taskset_path" == /* ]] || taskset_path="$PWD/$taskset_path"
    harbor_env+=("DATASET_NAME=auto" "DATASET_PATH=$taskset_path")
    ;;
  *) harbor_env+=("DATASET_NAME=$TASKSET") ;;
esac

[[ -z "$AGENT_ARG" ]] || harbor_env+=("AGENT=$AGENT_ARG")
[[ -z "$WORKERS" ]] || harbor_env+=("TOTAL_WORKERS=$WORKERS" "HARBOR_N_CONCURRENT=$WORKERS")
# FLEET_TASKS is an internal handoff owned by this CLI. Always override an
# inherited/configured value so omitting --task preserves full-taskset runs.
harbor_env+=("FLEET_TASKS=$FLEET_TASK")

# Assemble the full command in one always-non-empty array: expanding an
# empty array under `set -u` is an unbound-variable error on bash < 4.4
# (macOS /bin/bash 3.2), which broke every run without --detach.
harbor_cmd=(env "${harbor_env[@]}" bash "$REPO_DIR/Agents/utils/common/Harbor/start.sh")
(( VALIDATE_TASK_SELECTION == 0 )) || harbor_cmd+=(--validate-task-selection)
(( DETACH )) && harbor_cmd+=(--detach)
run_command "${harbor_cmd[@]}"
