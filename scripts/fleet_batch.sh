#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=fleet_spec_io.sh
source "$SCRIPT_DIR/fleet_spec_io.sh"

usage() {
  cat <<EOF
Usage:
  $0 --spec <spec.json> [spec.json ...] [--dry-run]

Each input may contain one FleetSpec v1 object or a non-empty array of them.
All specs are validated before any runner starts.
EOF
}

err() { printf '[ERROR] %s\n' "$*" >&2; }

is_openclaw_taskset() {
  [[ "$1" == "pinchbench" || "$1" == "clawbio" ]]
}

[[ "${1:-}" == "--spec" || "${1:-}" == "-s" ]] || {
  usage >&2
  exit 2
}
shift

INPUTS=()
DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) err "unknown internal multi-run option: $1"; usage >&2; exit 2 ;;
    *) INPUTS[${#INPUTS[@]}]="$1"; shift ;;
  esac
done
[[ ${#INPUTS[@]} -gt 0 ]] || { err "--spec requires at least one spec file"; exit 2; }

fleet_spec_load_many "${INPUTS[@]}" || exit $?

SPECS=()
TASKSETS=()
while IFS= read -r spec; do
  SPECS[${#SPECS[@]}]="$spec"
  TASKSETS[${#TASKSETS[@]}]="$(jq -r '.taskset' <<<"$spec")"
done < <(jq -c '.[]' <<<"$FLEET_SPECS_JSON")

TOTAL=${#SPECS[@]}
OPENCLAW_RUNS=0
for taskset in "${TASKSETS[@]}"; do
  if is_openclaw_taskset "$taskset"; then
    (( OPENCLAW_RUNS += 1 ))
  fi
done
if (( OPENCLAW_RUNS > 1 )); then
  err "batch supports at most one OpenClaw run (pinchbench or clawbio)"
  exit 2
fi
HARBOR_TASKSET_COUNT=$((TOTAL - OPENCLAW_RUNS))

for ((i = 0; i < TOTAL; i++)); do
  jq -e 'has("task")' <<<"${SPECS[$i]}" >/dev/null || continue
  task="$(jq -r '.task' <<<"${SPECS[$i]}")"
  status=0
  bash "$SCRIPT_DIR/run_fleet.sh" \
    --taskset "${TASKSETS[$i]}" "--task=$task" --validate-task-selection \
    || status=$?
  if (( status != 0 )); then
    err "task selection preflight failed for spec $((i + 1)): ${TASKSETS[$i]}"
    exit "$status"
  fi
done

BATCH_TIME="$(date +%Y%m%d-%H%M%S)"
BATCH_KEY="${BATCH_TIME}-$$"
ARTIFACT_ROOT="${FLEET_BATCH_LOG_DIR:-$PWD/fleet-batch-logs}"
ARTIFACT_DIR="$ARTIFACT_ROOT/$BATCH_KEY"
mkdir -p "$ARTIFACT_ROOT"
if ! mkdir "$ARTIFACT_DIR"; then
  err "cannot create batch artifact directory: $ARTIFACT_DIR"
  exit 1
fi

SPEC_FILES=()
LOG_FILES=()
RUN_IDS=()
for ((i = 0; i < TOTAL; i++)); do
  number=$((i + 1))
  # Keep the complete RUN_ID below Zellij's practical session-name limit. The
  # timestamp, PID, and sequence already provide uniqueness; the slug is only a
  # human-readable hint.
  slug="$(printf '%s' "${TASKSETS[$i]}" | LC_ALL=C tr -cs 'A-Za-z0-9_-' '-' | sed 's/^-*//; s/-*$//' | cut -c1-12)"
  [[ -n "$slug" ]] || slug="run"
  RUN_IDS[$i]="fleet-${BATCH_TIME}-$$-${number}-${slug}"
  SPEC_FILES[$i]="$ARTIFACT_DIR/${number}.spec.json"
  LOG_FILES[$i]="$ARTIFACT_DIR/${number}.log"
  fleet_spec_write "${SPEC_FILES[$i]}" "${SPECS[$i]}"
  (( DRY_RUN )) || : >"${LOG_FILES[$i]}"
done

# Harbor treats non-empty caller or config values as authoritative, including
# paths that would otherwise be derived from RUN_ID. Pass explicit empty values
# so env.sh re-derives all run state beneath this child's unique output path.
HARBOR_RUN_STATE_ENV=(
  "OUTPUT_PATH="
  "TASK_FILE="
  "QUEUE_DIR="
  "RUNTIME_DIR="
  "LAYOUT_FILE="
  "JOBS_ROOT="
  "HARBOR_ONLINE_ANALYSIS_DIR="
  "HARBOR_ONLINE_ANALYSIS_PID_FILE="
  "HARBOR_ONLINE_ANALYSIS_LOG_FILE="
  "HARBOR_MONITOR_DIR="
  "HARBOR_MONITOR_PID_FILE="
  "HARBOR_MONITOR_LOG_FILE="
  "HARBOR_BENCHMARK_PID_FILE="
  "HARBOR_BENCHMARK_EXIT_FILE="
  "HARBOR_JOB_DIR_FILE="
  "RL_TRACE_LOG="
  "RL_SERVER_LOG="
  "RL_SERVER_PID_FILE="
  "RL_QUEUE_DIR="
  "RL_ACTIVE_DIR="
  "RL_JOB_QUEUE_ROOT="
  "RL_JOB_RUNTIME_ROOT="
)

child_env() {
  local index="$1"
  CHILD_ENV=(
    "RUN_ID=${RUN_IDS[$index]}"
    "ZELLIJ_SESSION_NAME=${RUN_IDS[$index]}"
    "FLEET_BATCH_HARBOR_RUNS=$HARBOR_TASKSET_COUNT"
  )
  if ! is_openclaw_taskset "${TASKSETS[$index]}"; then
    CHILD_ENV+=("${HARBOR_RUN_STATE_ENV[@]}")
  fi
}

if (( DRY_RUN )); then
  failed=0
  for ((i = 0; i < TOTAL; i++)); do
    number=$((i + 1))
    printf '[%d/%d] %s RUN_ID=%s DRY-RUN\n' \
      "$number" "$TOTAL" "${TASKSETS[$i]}" "${RUN_IDS[$i]}" >&2
    child_env "$i"
    if ! env "${CHILD_ENV[@]}" \
      bash "$SCRIPT_DIR/run_fleet.sh" --spec "${SPEC_FILES[$i]}" --detach --dry-run; then
      failed=1
    fi
  done
  printf '[INFO] Batch artifacts: %s\n' "$ARTIFACT_DIR" >&2
  (( failed == 0 )) || exit 1
  exit 0
fi

PIDS=()
PID_COUNT=0

terminate_children() {
  local signal_name="$1" i pid
  trap - INT TERM HUP
  (( PID_COUNT > 0 )) || return 0
  for ((i = 0; i < PID_COUNT; i++)); do
    pid="${PIDS[$i]}"
    kill -0 "$pid" >/dev/null 2>&1 || continue
    # Children are process-group leaders (spawned under job control), so
    # signal the whole group: foreground runners like ClawBio do their work
    # in grandchildren, and signalling only the launcher PID orphans that
    # work mid-run.
    kill -s "$signal_name" -- "-$pid" >/dev/null 2>&1 \
      || kill -s "$signal_name" "$pid" >/dev/null 2>&1 || true
  done
  for ((i = 0; i < PID_COUNT; i++)); do
    wait "${PIDS[$i]}" >/dev/null 2>&1 || true
  done
}

handle_signal() {
  local signal_name="$1" status="$2"
  terminate_children "$signal_name"
  exit "$status"
}

trap 'handle_signal HUP 129' HUP
trap 'handle_signal INT 130' INT
trap 'handle_signal TERM 143' TERM

# Job control makes each background child the leader of its own process
# group, so cancellation can signal complete process trees instead of only
# the launcher PIDs.
set -m
for ((i = 0; i < TOTAL; i++)); do
  child_env "$i"
  env "${CHILD_ENV[@]}" \
    bash "$SCRIPT_DIR/run_fleet.sh" --spec "${SPEC_FILES[$i]}" --detach \
    >"${LOG_FILES[$i]}" 2>&1 &
  PIDS[$i]=$!
  PID_COUNT=$((PID_COUNT + 1))
done
set +m

failed=0
for ((i = 0; i < TOTAL; i++)); do
  number=$((i + 1))
  status=0
  if wait "${PIDS[$i]}"; then
    label="OK"
  else
    status=$?
    label="FAILED($status)"
    failed=1
  fi
  printf '[%d/%d] %s RUN_ID=%s %s log=%s\n' \
    "$number" "$TOTAL" "${TASKSETS[$i]}" "${RUN_IDS[$i]}" "$label" "${LOG_FILES[$i]}" >&2
done
printf '[INFO] Batch artifacts: %s\n' "$ARTIFACT_DIR" >&2

(( failed == 0 )) || exit 1
