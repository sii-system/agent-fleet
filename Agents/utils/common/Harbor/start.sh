#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export RUN_ID="${RUN_ID:-$(date +%Y-%m-%d-%H%M)-harbor-tui}"
. "$SCRIPT_DIR/env.sh"

if [[ "${1:-}" == "--validate-task-selection" ]]; then
  [[ $# -eq 1 ]] || {
    printf '[ERROR] --validate-task-selection does not accept other arguments\n' >&2
    exit 2
  }
  harbor_validate_task_selection
  exit
fi

if [[ "$ROLLOUT" == "1" && "${FLEET_BATCH_HARBOR_RUNS:-1}" != "1" ]]; then
  printf '[ERROR] ROLLOUT=1 supports only one Harbor run per Batch; rollout listeners share RL_PORT=%s\n' \
    "$RL_PORT" >&2
  exit 2
fi

DETACH_MODE=false
if [[ "${1:-}" == "--detach" ]]; then
  DETACH_MODE=true
  shift
fi
if [[ "${HARBOR_FIXER_VERIFICATION_RERUN:-0}" == "1" ]]; then
  # Fixer verification owns monitoring and must not inherit or create a
  # benchmark Zellij session. A bare start.sh invocation runs Harbor directly.
  DETACH_MODE=false
  if [[ $# -eq 0 ]]; then
    set -- "$SCRIPT_DIR/harboropik.sh"
  fi
fi
if [[ -z "$HARBOR_ZELLIJ_KEEP_ON_FAILURE" ]]; then
  if [[ "$DETACH_MODE" == "true" || ( -t 0 && -t 1 ) ]]; then
    HARBOR_ZELLIJ_KEEP_ON_FAILURE=1
  else
    HARBOR_ZELLIJ_KEEP_ON_FAILURE=0
  fi
fi
case "$HARBOR_ZELLIJ_KEEP_ON_FAILURE" in
  0|1) export HARBOR_ZELLIJ_KEEP_ON_FAILURE ;;
  *)
    printf '[ERROR] HARBOR_ZELLIJ_KEEP_ON_FAILURE must be 0 or 1\n' >&2
    exit 2
    ;;
esac

# Explicit names still win for normal benchmark zellij sessions.
ZELLIJ_SESSION_NAME="${ZELLIJ_SESSION_NAME:-${RL_ZELLIJ_SESSION_NAME:-$HARBOR_ZELLIJ_SESSION_NAME}}"

harbor_print_run_receipt() {
  printf '[RUN] status: starting\n'
  printf '[RUN] RUN_ID: %s\n' "$RUN_ID"
  printf '[RUN] Zellij session: %s\n' "$ZELLIJ_SESSION_NAME"
  printf '[RUN] output: %s\n' "$OUTPUT_PATH"
  printf '[RUN] summary: %s/summary.txt\n' "$OUTPUT_PATH"
}

harbor_report_foreground_result() {
  local zellij_status="$1"
  local benchmark_status=""

  echo
  if [[ -f "$OUTPUT_PATH/summary.txt" ]]; then
    cat "$OUTPUT_PATH/summary.txt"
  else
    echo "[ERROR] summary unavailable: $OUTPUT_PATH/summary.txt" >&2
  fi

  if harbor_uses_registry_dataset; then
    if [[ -s "$HARBOR_BENCHMARK_EXIT_FILE" ]]; then
      benchmark_status="$(cat "$HARBOR_BENCHMARK_EXIT_FILE" 2>/dev/null || true)"
    fi
    if [[ "$benchmark_status" =~ ^([0-9]|[1-9][0-9]|1[0-9][0-9]|2[0-4][0-9]|25[0-5])$ ]]; then
      return "$benchmark_status"
    fi
  elif [[ -f "$OUTPUT_PATH/summary.txt" && "$zellij_status" -eq 0 ]]; then
    return 0
  fi
  if [[ "$zellij_status" -ne 0 ]]; then
    echo "[ERROR] Zellij exited with status $zellij_status before Harbor recorded completion." >&2
    return "$zellij_status"
  fi
  echo "[ERROR] Zellij ended before Harbor recorded a completion status." >&2
  return 1
}

harbor_stop_rollout_zellij_sessions() {
  local session
  while IFS= read -r session; do
    if [[ "$session" == harbor-rollout-* || "$session" =~ ^hr-[0-9a-f]{32}$ ]]; then
      zellij kill-session "$session" >/dev/null 2>&1 || true
      zellij delete-session "$session" >/dev/null 2>&1 || true
    fi
  done < <(zellij list-sessions --short 2>/dev/null || true)
}

ensure_zellij_web_sharing_config() {
  local config_file="${ZELLIJ_CONFIG_FILE:-$HOME/.config/zellij/config.kdl}"
  mkdir -p "$(dirname "$config_file")"
  if [[ -f "$config_file" ]] && grep -qE '^[[:space:]]*web_sharing[[:space:]]+' "$config_file"; then
    sed -i -E 's/^[[:space:]]*web_sharing[[:space:]]+".*"$/web_sharing "on"/' "$config_file"
  else
    printf '\nweb_sharing "on"\n' >> "$config_file"
  fi
}

harbor_start_online_analysis_if_enabled() {
  if [[ "$HARBOR_ONLINE_ANALYSIS" != "1" ]]; then
    return 0
  fi
  if [[ -f "$HARBOR_ONLINE_ANALYSIS_PID_FILE" ]]; then
    local existing_pid
    existing_pid="$(cat "$HARBOR_ONLINE_ANALYSIS_PID_FILE" 2>/dev/null || true)"
    if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" >/dev/null 2>&1; then
      return 0
    fi
  fi

  mkdir -p "$HARBOR_ONLINE_ANALYSIS_DIR" "$RUNTIME_DIR"
  local online_analysis_profile
  online_analysis_profile="$(harbor_dataset_kind)"
  nohup setsid python3 "$SCRIPT_DIR/scripts/online_rule_analyzer.py" \
    "$OUTPUT_PATH" \
    --follow \
    --profile "$online_analysis_profile" \
    --poll-interval "$HARBOR_ONLINE_ANALYSIS_POLL_INTERVAL" \
    --output-dir "$HARBOR_ONLINE_ANALYSIS_DIR" \
    >>"$HARBOR_ONLINE_ANALYSIS_LOG_FILE" 2>&1 &
  printf '%s\n' "$!" >"$HARBOR_ONLINE_ANALYSIS_PID_FILE"
}

harbor_start_monitor_if_enabled() {
  [[ "$HARBOR_MONITOR_ENABLED" == "1" ]] || return 0
  [[ "$ROLLOUT" != "1" ]] || return 0
  if ! harbor_uses_registry_dataset && [[ ! -s "$TASK_FILE" ]]; then
    echo "[ERROR] cannot start Harbor monitor without a materialized task file: $TASK_FILE" >&2
    return 1
  fi
  mkdir -p "$HARBOR_MONITOR_DIR" "$RUNTIME_DIR"
  (
  flock 9
  if [[ -f "$HARBOR_MONITOR_PID_FILE" ]]; then
    local existing_pid
    existing_pid="$(cat "$HARBOR_MONITOR_PID_FILE" 2>/dev/null || true)"
    if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" >/dev/null 2>&1; then
      if harbor_monitor_pid_matches_run "$existing_pid"; then
        exit 0
      fi
      echo "[ERROR] refusing to replace unrelated process from $HARBOR_MONITOR_PID_FILE: pid=$existing_pid" >&2
      exit 1
    fi
    rm -f "$HARBOR_MONITOR_PID_FILE"
  fi

  local -a monitor_args=(
    --run-dir "$OUTPUT_PATH"
    --queue-dir "$QUEUE_DIR"
    --agent "$AGENT"
    --output "$HARBOR_MONITOR_DIR/monitor-latest.json"
    --user-report-output "$HARBOR_MONITOR_DIR/user-notify-latest.json"
    --analyzer-handover-output "$HARBOR_MONITOR_DIR/analyzer-handover-latest.json"
    --runner-action-output "$HARBOR_MONITOR_DIR/runner-action-latest.json"
    --follow
    --interval "$HARBOR_MONITOR_INTERVAL"
    --startup-grace "$HARBOR_MONITOR_STARTUP_GRACE"
    --stall-seconds "$HARBOR_MONITOR_STALL_SECONDS"
    --max-retries "$HARBOR_MONITOR_MAX_RETRIES"
  )
  if harbor_uses_registry_dataset; then
    monitor_args+=(
      --harbor-job-dir-file "$HARBOR_JOB_DIR_FILE"
      --harbor-pid-file "$HARBOR_BENCHMARK_PID_FILE"
      --harbor-exit-file "$HARBOR_BENCHMARK_EXIT_FILE"
    )
  else
    monitor_args+=(--task-file "$TASK_FILE")
  fi
  if [[ -n "$HARBOR_MONITOR_CONFIGURED_TIMEOUT" ]]; then
    monitor_args+=(--configured-timeout "$HARBOR_MONITOR_CONFIGURED_TIMEOUT")
  fi
  if [[ -n "$HARBOR_MONITOR_RESTART_CMD" ]]; then
    monitor_args+=(--restart-cmd "$HARBOR_MONITOR_RESTART_CMD")
  fi
  if [[ -n "$HARBOR_MONITOR_STOP_CMD" ]]; then
    monitor_args+=(--stop-cmd "$HARBOR_MONITOR_STOP_CMD")
  fi
  nohup setsid python3 "$SCRIPT_DIR/scripts/monitor.py" "${monitor_args[@]}" 9>&- \
    >>"$HARBOR_MONITOR_LOG_FILE" 2>&1 &
  local monitor_pid="$!"
  printf '%s\n' "$monitor_pid" > "$HARBOR_MONITOR_PID_FILE"
  for _ in $(seq 1 50); do
    [[ -f "$HARBOR_MONITOR_DIR/monitor-latest.json" ]] && exit 0
    if ! kill -0 "$monitor_pid" >/dev/null 2>&1; then
      echo "[ERROR] Harbor monitor exited during startup; see $HARBOR_MONITOR_LOG_FILE" >&2
      exit 1
    fi
    sleep 0.1
  done
  echo "[ERROR] Harbor monitor did not produce a startup sample; see $HARBOR_MONITOR_LOG_FILE" >&2
  exit 1
  ) 9>"$RUNTIME_DIR/harbor-monitor.lock"
}

harbor_start_analyzer_if_enabled() {
  [[ "$HARBOR_ANALYZER_ENABLED" == "1" ]] || return 0
  [[ "$ROLLOUT" != "1" ]] || return 0
  if [[ "$HARBOR_MONITOR_ENABLED" != "1" ]]; then
    echo "[ERROR] cannot start Harbor analyzer because Harbor monitor is disabled" >&2
    return 1
  fi

  (
  flock 9
  if [[ -f "$HARBOR_ANALYZER_PID_FILE" ]]; then
    local existing_pid
    existing_pid="$(cat "$HARBOR_ANALYZER_PID_FILE" 2>/dev/null || true)"
    if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" >/dev/null 2>&1; then
      if harbor_analyzer_pid_matches_run "$existing_pid"; then
        exit 0
      fi
      echo "[ERROR] refusing to replace unrelated process from $HARBOR_ANALYZER_PID_FILE: pid=$existing_pid" >&2
      exit 1
    fi
    rm -f "$HARBOR_ANALYZER_PID_FILE"
  fi

  for _ in $(seq 1 50); do
    [[ -f "$HARBOR_MONITOR_DIR/analyzer-handover-latest.json" ]] && break
    sleep 0.1
  done
  if [[ ! -f "$HARBOR_MONITOR_DIR/analyzer-handover-latest.json" ]]; then
    echo "[ERROR] cannot start Harbor analyzer before monitor handover exists" >&2
    exit 1
  fi

  mkdir -p "$HARBOR_ANALYZER_OUTPUT_DIR" "$RUNTIME_DIR"
  local analyzer_pid ready_file
  ready_file="$HARBOR_ANALYZER_OUTPUT_DIR/.analyzer-ready"
  rm -f "$ready_file"
  case "$HARBOR_ANALYZER_MODE" in
    handover-follow)
      nohup setsid python3 "$SCRIPT_DIR/scripts/analyzer_subagent.py" \
        --handover "$HARBOR_MONITOR_DIR/analyzer-handover-latest.json" \
        --handoff-dir "$HARBOR_MONITOR_DIR/analyzer-handoffs" \
        --run-dir "$OUTPUT_PATH" \
        --run-id "$RUN_ID" \
        --queue-dir "$QUEUE_DIR" \
        --agent "$AGENT" \
        --output-dir "$HARBOR_ANALYZER_OUTPUT_DIR" \
        --pi-provider "$HARBOR_ANALYZER_PI_PROVIDER" \
        --pi-model "$HARBOR_ANALYZER_MODEL" \
        --pi-base-url "$HARBOR_ANALYZER_BASE_URL" \
        --pi-api-key-env HARBOR_ANALYZER_API_KEY \
        --timeout "$HARBOR_ANALYZER_TIMEOUT" \
        --max-concurrency "$HARBOR_ANALYZER_MAX_CONCURRENCY" \
        --ready-file "$ready_file" \
        --follow \
        --poll-interval "$HARBOR_ANALYZER_POLL_INTERVAL" \
        >>"$HARBOR_ANALYZER_LOG_FILE" 2>&1 9>&- &
      analyzer_pid="$!"
      ;;
    *)
      echo "[ERROR] unsupported HARBOR_ANALYZER_MODE=$HARBOR_ANALYZER_MODE" >&2
      exit 1
      ;;
  esac
  printf '%s\n' "$analyzer_pid" >"$HARBOR_ANALYZER_PID_FILE"
  for _ in $(seq 1 50); do
    if ! kill -0 "$analyzer_pid" >/dev/null 2>&1; then
      rm -f "$HARBOR_ANALYZER_PID_FILE" "$ready_file"
      echo "[ERROR] Harbor analyzer exited during startup; see $HARBOR_ANALYZER_LOG_FILE" >&2
      exit 1
    fi
    if [[ -f "$ready_file" ]] && grep -qx "$analyzer_pid" "$ready_file"; then
      exit 0
    fi
    sleep 0.1
  done
  harbor_stop_analyzer "$analyzer_pid" >/dev/null 2>&1 || true
  rm -f "$HARBOR_ANALYZER_PID_FILE" "$ready_file"
  echo "[ERROR] Harbor analyzer did not become ready during startup; see $HARBOR_ANALYZER_LOG_FILE" >&2
  exit 1
  ) 9>"$RUNTIME_DIR/harbor-analyzer.lock"
}

harbor_monitor_is_running_for_run() {
  [[ "$HARBOR_MONITOR_ENABLED" == "1" && -f "$HARBOR_MONITOR_PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$HARBOR_MONITOR_PID_FILE" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" >/dev/null 2>&1 || return 1
  harbor_monitor_pid_matches_run "$pid"
}

harbor_wait_for_monitor_completion() {
  [[ "$HARBOR_MONITOR_ENABLED" == "1" && "$ROLLOUT" != "1" ]] || return 0
  harbor_monitor_is_running_for_run || return 0
  local timeout deadline
  timeout="$(harbor_analyzer_shutdown_timeout)"
  deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    harbor_monitor_is_running_for_run || return 0
    sleep 1
  done
  echo "[WARN] Harbor monitor still running after ${timeout}s analyzer shutdown wait; stopping analyzer anyway" >&2
}

harbor_analyzer_pending_drained() {
  [[ -f "$HARBOR_MONITOR_DIR/analyzer-handover-latest.json" ]] || return 0
  python3 - "$SCRIPT_DIR/scripts" \
    "$HARBOR_MONITOR_DIR/analyzer-handover-latest.json" \
    "$HARBOR_MONITOR_DIR/analyzer-handoffs" \
    "$HARBOR_ANALYZER_OUTPUT_DIR/.analyzer_state.json" <<'PY'
import sys
import time
from pathlib import Path

scripts_dir = Path(sys.argv[1])
sys.path.insert(0, str(scripts_dir))
from analyzer_subagent import _load_follow_state, _pending_handovers  # noqa: E402

latest_path = Path(sys.argv[2])
handoff_dir = Path(sys.argv[3])
state_path = Path(sys.argv[4])
processed, failed = _load_follow_state(state_path)
pending = _pending_handovers(
    latest_path=latest_path,
    handoff_dir=handoff_dir,
    processed=processed,
    failed=failed,
    now=time.time(),
    include_deferred_retries=True,
)
raise SystemExit(0 if not pending else 1)
PY
}

harbor_wait_for_analyzer_drain() {
  [[ "$HARBOR_ANALYZER_ENABLED" == "1" && "$ROLLOUT" != "1" ]] || return 0
  [[ -f "$HARBOR_ANALYZER_PID_FILE" ]] || return 0
  local expected_pid="${1:-}" current_pid pid timeout deadline
  current_pid="$(cat "$HARBOR_ANALYZER_PID_FILE" 2>/dev/null || true)"
  pid="${expected_pid:-$current_pid}"
  if [[ -n "$expected_pid" && "$current_pid" != "$expected_pid" ]]; then
    [[ "$expected_pid" =~ ^[0-9]+$ ]] || return 0
  fi
  [[ "$pid" =~ ^[0-9]+$ ]] || return 0
  kill -0 "$pid" >/dev/null 2>&1 || return 0
  timeout="$(harbor_analyzer_shutdown_timeout)"
  deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    kill -0 "$pid" >/dev/null 2>&1 || return 0
    if harbor_analyzer_pending_drained; then
      return 0
    fi
    sleep 1
  done
  echo "[WARN] Harbor analyzer still has pending handovers after ${timeout}s drain wait; stopping it" >&2
}

harbor_write_benchmark_summary() {
  python3 "$SCRIPT_DIR/scripts/write_benchmark_summary.py" \
    "$HARBOR_MONITOR_DIR/monitor-latest.json" \
    "$HARBOR_ANALYZER_OUTPUT_DIR/analyzer-artifacts-latest.json" \
    "$HARBOR_ANALYZER_OUTPUT_DIR/benchmark-summary.md" "$RUN_ID" \
    "$OUTPUT_PATH/fixer/fix-report-latest.md" \
    || echo "[WARN] failed to write Harbor benchmark summary" >&2
}

harbor_finish_analyzer_lifecycle() {
  [[ "$HARBOR_ANALYZER_ENABLED" == "1" && "$ROLLOUT" != "1" ]] || return 0
  harbor_wait_for_monitor_completion
  harbor_wait_for_analyzer_drain
  harbor_stop_analyzer || true
  harbor_write_benchmark_summary
}

harbor_analyzer_shutdown_timeout() {
  local configured_timeout analyzer_timeout max_concurrency poll_interval derived_timeout
  configured_timeout="${HARBOR_ANALYZER_DRAIN_TIMEOUT:-0}"
  analyzer_timeout="${HARBOR_ANALYZER_TIMEOUT:-900}"
  max_concurrency="${HARBOR_ANALYZER_MAX_CONCURRENCY:-1}"
  poll_interval="${HARBOR_ANALYZER_POLL_INTERVAL:-5}"
  [[ "$configured_timeout" =~ ^[0-9]+$ ]] || configured_timeout=0
  [[ "$analyzer_timeout" =~ ^[0-9]+$ ]] || analyzer_timeout=900
  [[ "$max_concurrency" =~ ^[0-9]+$ && "$max_concurrency" -gt 0 ]] || max_concurrency=1
  derived_timeout="$analyzer_timeout"
  if [[ -f "$HARBOR_MONITOR_DIR/analyzer-handover-latest.json" ]]; then
    derived_timeout="$(
      python3 - "$SCRIPT_DIR/scripts" \
        "$HARBOR_MONITOR_DIR/analyzer-handover-latest.json" \
        "$HARBOR_MONITOR_DIR/analyzer-handoffs" \
        "$HARBOR_ANALYZER_OUTPUT_DIR/.analyzer_state.json" \
        "$analyzer_timeout" \
        "$max_concurrency" \
        "$poll_interval" <<'PY'
import sys
import time
from pathlib import Path

scripts_dir = Path(sys.argv[1])
sys.path.insert(0, str(scripts_dir))
from analyzer_subagent import analyzer_drain_budget_seconds  # noqa: E402

latest_path = Path(sys.argv[2])
handoff_dir = Path(sys.argv[3])
state_path = Path(sys.argv[4])
timeout = int(sys.argv[5])
max_concurrency = max(1, int(sys.argv[6]))
poll_interval = float(sys.argv[7])
print(analyzer_drain_budget_seconds(
    latest_path=latest_path,
    handoff_dir=handoff_dir,
    state_path=state_path,
    timeout_seconds=timeout,
    max_concurrency=max_concurrency,
    poll_interval_seconds=poll_interval,
    now=time.time(),
))
PY
	    )" || derived_timeout="$analyzer_timeout"
  fi
  [[ "$derived_timeout" =~ ^[0-9]+$ ]] || derived_timeout="$analyzer_timeout"
  (( configured_timeout > derived_timeout )) && printf '%s\n' "$configured_timeout" || printf '%s\n' "$derived_timeout"
}

harbor_start_detached_analyzer_supervisor_if_enabled() {
  [[ "$HARBOR_ANALYZER_ENABLED" == "1" && "$ROLLOUT" != "1" ]] || return 0
  [[ -f "$HARBOR_ANALYZER_PID_FILE" ]] || return 0
  local analyzer_pid
  analyzer_pid="$(cat "$HARBOR_ANALYZER_PID_FILE" 2>/dev/null || true)"
  [[ "$analyzer_pid" =~ ^[0-9]+$ ]] || return 0
  kill -0 "$analyzer_pid" >/dev/null 2>&1 || return 0
  if ! harbor_analyzer_pid_matches_run "$analyzer_pid"; then
    echo "[ERROR] refusing to supervise unrelated process from $HARBOR_ANALYZER_PID_FILE: pid=$analyzer_pid" >&2
    return 1
  fi
  (
  flock 9
  if [[ -f "$HARBOR_ANALYZER_SUPERVISOR_PID_FILE" ]]; then
    local existing_pid
    existing_pid="$(cat "$HARBOR_ANALYZER_SUPERVISOR_PID_FILE" 2>/dev/null || true)"
    if [[ "$existing_pid" =~ ^[0-9]+$ ]] && kill -0 "$existing_pid" >/dev/null 2>&1; then
      if harbor_analyzer_supervisor_pid_matches_run "$existing_pid" "$analyzer_pid"; then
        exit 0
      fi
      if ! harbor_analyzer_supervisor_pid_matches_run "$existing_pid"; then
        rm -f "$HARBOR_ANALYZER_SUPERVISOR_PID_FILE" "$HARBOR_ANALYZER_SUPERVISOR_ID_FILE"
      else
        harbor_stop_analyzer_supervisor || exit 1
      fi
    else
      rm -f "$HARBOR_ANALYZER_SUPERVISOR_PID_FILE" "$HARBOR_ANALYZER_SUPERVISOR_ID_FILE"
    fi
  fi
  (
    exec 9>&-
    trap '' HUP
    while harbor_monitor_is_running_for_run; do
      sleep 5
    done
    harbor_wait_for_analyzer_drain "$analyzer_pid"
    harbor_stop_analyzer "$analyzer_pid" || true
    harbor_write_benchmark_summary
  ) >>"$HARBOR_ANALYZER_LOG_FILE" 2>&1 &
  local supervisor_pid="$!"
  if ! harbor_write_analyzer_supervisor_identity "$supervisor_pid" "$analyzer_pid"; then
    kill "$supervisor_pid" >/dev/null 2>&1 || true
    rm -f "$HARBOR_ANALYZER_SUPERVISOR_PID_FILE" "$HARBOR_ANALYZER_SUPERVISOR_ID_FILE"
    echo "[ERROR] failed to record Harbor analyzer supervisor identity" >&2
    exit 1
  fi
  printf '%s\n' "$supervisor_pid" >"$HARBOR_ANALYZER_SUPERVISOR_PID_FILE"
  ) 9>"$RUNTIME_DIR/harbor-analyzer-supervisor.lock"
}

harbor_rollback_analyzer_startup() {
  harbor_stop_analyzer >/dev/null 2>&1 || true
  harbor_stop_monitor >/dev/null 2>&1 || true
  harbor_stop_online_analysis >/dev/null 2>&1 || true
}

harbor_validate_task_selection

harbor_init_run_dirs
if [[ "$ROLLOUT" != "1" ]] && harbor_uses_registry_dataset; then
  : > "$HARBOR_JOB_DIR_FILE"
  rm -f "$HARBOR_BENCHMARK_PID_FILE" "$HARBOR_BENCHMARK_EXIT_FILE"
fi
if [[ "$ROLLOUT" != "1" ]]; then
  harbor_validate_agent
  harbor_validate_generation_controls
  harbor_ensure_dataset
else
  mkdir -p "$RL_TRIALS_DIR" "$RL_ACTIVE_DIR" "$RL_QUEUE_DIR/pending" "$RL_QUEUE_DIR/results" "$RL_JOB_QUEUE_ROOT" "$RL_JOB_RUNTIME_ROOT" "$(dirname "$RL_TRACE_LOG")"
  touch "$RL_TRACE_LOG"
fi
if [[ "${RESET_RUN:-0}" == "1" ]]; then
  if [[ "$ROLLOUT" == "1" ]]; then
    "$RL_UTILS_DIR/run_rl_rollout_server.sh" --stop >/dev/null 2>&1 || true
    harbor_stop_rollout_zellij_sessions
  elif [[ "${HARBOR_FIXER_VERIFICATION_RERUN:-0}" != "1" ]]; then
    zellij kill-session "$ZELLIJ_SESSION_NAME" >/dev/null 2>&1 || true
    zellij delete-session "$ZELLIJ_SESSION_NAME" >/dev/null 2>&1 || true
  fi
  harbor_reset_run_state
  if [[ "$ROLLOUT" == "1" ]]; then
    rm -rf "$RL_JOB_QUEUE_ROOT" "$RL_JOB_RUNTIME_ROOT" 2>/dev/null || true
    mkdir -p "$RL_JOB_QUEUE_ROOT" "$RL_JOB_RUNTIME_ROOT"
    rm -f "$RL_ACTIVE_DIR"/*.json "$RL_QUEUE_DIR"/pending/*.json "$RL_QUEUE_DIR"/results/*.json "$RL_TRACE_LOG" "$RL_SERVER_LOG" 2>/dev/null || true
    touch "$RL_TRACE_LOG"
  fi
fi

if [[ $# -gt 0 ]]; then
  if [[ "$ROLLOUT" != "1" ]] && ! harbor_uses_registry_dataset; then
    harbor_prepare_task_file
    export RESET_RUN=0
  fi
  if [[ "$ROLLOUT" != "1" ]]; then
    harbor_start_online_analysis_if_enabled
    harbor_start_monitor_if_enabled
    if ! harbor_start_analyzer_if_enabled; then
      harbor_rollback_analyzer_startup
      exit 1
    fi
    trap 'harbor_finish_analyzer_lifecycle' EXIT
  fi
  command_pid=""
  command_signal=""
  trap 'command_signal=TERM; [[ -z "$command_pid" ]] || kill -TERM "$command_pid" 2>/dev/null || true' TERM
  trap 'command_signal=INT; [[ -z "$command_pid" ]] || kill -INT "$command_pid" 2>/dev/null || true' INT
  trap 'command_signal=HUP; [[ -z "$command_pid" ]] || kill -HUP "$command_pid" 2>/dev/null || true' HUP
  (
    trap - TERM INT HUP
    exec "$@"
  ) <&0 &
  command_pid="$!"
  if [[ -n "$command_signal" ]]; then
    kill "-$command_signal" "$command_pid" 2>/dev/null || true
  fi
  set +e
  wait "$command_pid"
  command_rc="$?"
  if [[ -n "$command_signal" ]] && kill -0 "$command_pid" 2>/dev/null; then
    wait "$command_pid"
    command_rc="$?"
  fi
  set -e
  trap - TERM INT HUP
  if [[ -n "$command_signal" ]]; then
    trap - EXIT
    harbor_rollback_analyzer_startup
    kill "-$command_signal" "$$"
  fi
  harbor_finish_analyzer_lifecycle
  trap - EXIT
  exit "$command_rc"
fi

if [[ "$ROLLOUT" != "1" ]] && ! harbor_uses_registry_dataset; then
  harbor_prepare_task_file
fi
export RESET_RUN=0
if [[ "$ROLLOUT" != "1" ]]; then
  harbor_start_online_analysis_if_enabled
fi
cd "$SCRIPT_DIR"

if [[ "$ROLLOUT" == "1" ]]; then
  if [[ "$DETACH_MODE" == "true" ]]; then
    exec "$RL_UTILS_DIR/run_rl_rollout_server.sh" --detach
  fi
  exec "$RL_UTILS_DIR/run_rl_rollout_server.sh"
fi

if harbor_uses_registry_dataset; then
  "$SCRIPT_DIR/gen_harbor_registry_zellij_layout.sh" "$LAYOUT_FILE"
else
  "$SCRIPT_DIR/gen_harbor_zellij_layout.sh" "$LAYOUT_FILE"
fi
ensure_zellij_web_sharing_config

if [[ "$DETACH_MODE" == "true" ]]; then
  zellij kill-session "$ZELLIJ_SESSION_NAME" >/dev/null 2>&1 || true
  zellij delete-session "$ZELLIJ_SESSION_NAME" >/dev/null 2>&1 || true
  zellij_cmd="$(printf 'stty rows 54 cols 172; exec zellij --session %q --new-session-with-layout %q' "$ZELLIJ_SESSION_NAME" "$LAYOUT_FILE")"
  nohup setsid env -u ZELLIJ_SESSION_NAME TERM=xterm-256color script -q \
    -c "$zellij_cmd" \
    "$RUNTIME_DIR/zellij-${ZELLIJ_SESSION_NAME}.typescript" \
    >"$RUNTIME_DIR/zellij-${ZELLIJ_SESSION_NAME}.log" 2>&1 &

  started=false
  for _ in $(seq 1 30); do
    if zellij list-sessions --short 2>/dev/null | grep -qx "$ZELLIJ_SESSION_NAME"; then
      started=true
      break
    fi
    sleep 1
  done

  if [[ "$started" != "true" ]]; then
    echo "failed to create zellij session: $ZELLIJ_SESSION_NAME" >&2
    echo "zellij log: $RUNTIME_DIR/zellij-${ZELLIJ_SESSION_NAME}.log" >&2
    exit 1
  fi

  if ! harbor_start_monitor_if_enabled; then
    zellij kill-session "$ZELLIJ_SESSION_NAME" >/dev/null 2>&1 || true
    zellij delete-session "$ZELLIJ_SESSION_NAME" >/dev/null 2>&1 || true
    exit 1
  fi
  if ! harbor_start_analyzer_if_enabled; then
    harbor_rollback_analyzer_startup
    zellij kill-session "$ZELLIJ_SESSION_NAME" >/dev/null 2>&1 || true
    zellij delete-session "$ZELLIJ_SESSION_NAME" >/dev/null 2>&1 || true
    exit 1
  fi
  if ! harbor_start_detached_analyzer_supervisor_if_enabled; then
    harbor_rollback_analyzer_startup
    zellij kill-session "$ZELLIJ_SESSION_NAME" >/dev/null 2>&1 || true
    zellij delete-session "$ZELLIJ_SESSION_NAME" >/dev/null 2>&1 || true
    exit 1
  fi

  harbor_print_run_receipt
  printf '[RUN] attach: zellij attach %s\n' "$ZELLIJ_SESSION_NAME"
  printf '%s\n' "$ZELLIJ_SESSION_NAME"
  exit 0
fi

harbor_start_monitor_if_enabled
if ! harbor_start_analyzer_if_enabled; then
  harbor_rollback_analyzer_startup
  exit 1
fi
trap 'harbor_finish_analyzer_lifecycle' EXIT
trap 'zellij kill-session "$ZELLIJ_SESSION_NAME" >/dev/null 2>&1 || true; exit 129' HUP
trap 'zellij kill-session "$ZELLIJ_SESSION_NAME" >/dev/null 2>&1 || true; exit 130' INT
trap 'zellij kill-session "$ZELLIJ_SESSION_NAME" >/dev/null 2>&1 || true; exit 143' TERM
harbor_print_run_receipt
zellij_status=0
env -u ZELLIJ_SESSION_NAME zellij \
  --session "$ZELLIJ_SESSION_NAME" \
  --new-session-with-layout "$LAYOUT_FILE" <&0 &
wait "$!" || zellij_status="$?"
harbor_finish_analyzer_lifecycle
trap - EXIT HUP INT TERM
final_status=0
harbor_report_foreground_result "$zellij_status" || final_status="$?"
exit "$final_status"
