#!/usr/bin/env bash
set -euo pipefail

# Run directories, process identity, shutdown, and reset.
# Sourced by ../env.sh; SCRIPT_DIR remains the Harbor directory.

harbor_init_run_dirs() {
  mkdir -p "$OUTPUT_PATH" "$QUEUE_DIR" "$RUNTIME_DIR/worker-logs" "$JOBS_ROOT"
  touch "$QUEUE_DIR/done.txt" "$QUEUE_DIR/failed.txt"
}

harbor_analyzer_pid_matches_run() {
  local pid="$1" arg previous="" script_seen=0 run_seen=0 handover_seen=0
  [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/cmdline" ]] || return 1
  while IFS= read -r -d '' arg; do
    [[ "$arg" == "$SCRIPT_DIR/scripts/analyzer_subagent.py" ]] && script_seen=1
    [[ "$previous" == "--run-dir" && "$arg" == "$OUTPUT_PATH" ]] && run_seen=1
    [[ "$previous" == "--handover" && "$arg" == "$HARBOR_MONITOR_DIR/analyzer-handover-latest.json" ]] && handover_seen=1
    previous="$arg"
  done < "/proc/$pid/cmdline"
  [[ "$script_seen" == 1 && "$run_seen" == 1 && "$handover_seen" == 1 ]]
}

harbor_process_start_time() {
  local pid="$1"
  [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/stat" ]] || return 1
  awk '{print $22}' "/proc/$pid/stat" 2>/dev/null
}

harbor_identity_file_value() {
  local file="$1" key="$2" line_key line_value
  [[ -f "$file" ]] || return 1
  while IFS='=' read -r line_key line_value; do
    if [[ "$line_key" == "$key" ]]; then
      printf '%s\n' "$line_value"
      return 0
    fi
  done < "$file"
  return 1
}

harbor_write_analyzer_supervisor_identity() {
  local pid="$1" analyzer_pid="$2" start_time analyzer_start_time
  start_time="$(harbor_process_start_time "$pid")" || return 1
  analyzer_start_time="$(harbor_process_start_time "$analyzer_pid")" || return 1
  {
    printf 'pid=%s\n' "$pid"
    printf 'start_time=%s\n' "$start_time"
    printf 'run_dir=%s\n' "$OUTPUT_PATH"
    printf 'analyzer_pid=%s\n' "$analyzer_pid"
    printf 'analyzer_start_time=%s\n' "$analyzer_start_time"
  } > "$HARBOR_ANALYZER_SUPERVISOR_ID_FILE"
}

harbor_analyzer_supervisor_pid_matches_run() {
  local pid="$1" expected_analyzer_pid="${2:-}" stored_pid start_time run_dir analyzer_pid analyzer_start_time
  local current_start_time current_analyzer_start_time
  [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/stat" ]] || return 1
  stored_pid="$(harbor_identity_file_value "$HARBOR_ANALYZER_SUPERVISOR_ID_FILE" pid || true)"
  start_time="$(harbor_identity_file_value "$HARBOR_ANALYZER_SUPERVISOR_ID_FILE" start_time || true)"
  run_dir="$(harbor_identity_file_value "$HARBOR_ANALYZER_SUPERVISOR_ID_FILE" run_dir || true)"
  analyzer_pid="$(harbor_identity_file_value "$HARBOR_ANALYZER_SUPERVISOR_ID_FILE" analyzer_pid || true)"
  analyzer_start_time="$(harbor_identity_file_value "$HARBOR_ANALYZER_SUPERVISOR_ID_FILE" analyzer_start_time || true)"
  [[ "$stored_pid" == "$pid" && "$run_dir" == "$OUTPUT_PATH" ]] || return 1
  current_start_time="$(harbor_process_start_time "$pid")" || return 1
  [[ -n "$start_time" && "$start_time" == "$current_start_time" ]] || return 1
  [[ -z "$expected_analyzer_pid" || "$analyzer_pid" == "$expected_analyzer_pid" ]] || return 1
  if [[ -n "$expected_analyzer_pid" ]]; then
    [[ "$analyzer_pid" =~ ^[0-9]+$ && -r "/proc/$analyzer_pid/stat" ]] || return 1
    current_analyzer_start_time="$(harbor_process_start_time "$analyzer_pid")" || return 1
    [[ -n "$analyzer_start_time" && "$analyzer_start_time" == "$current_analyzer_start_time" ]] || return 1
    harbor_analyzer_pid_matches_run "$analyzer_pid"
  fi
}

harbor_monitor_pid_matches_run() {
  local pid="$1" arg previous="" script_seen=0 run_seen=0
  [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/cmdline" ]] || return 1
  while IFS= read -r -d '' arg; do
    [[ "$arg" == "$SCRIPT_DIR/scripts/monitor.py" ]] && script_seen=1
    [[ "$previous" == "--run-dir" && "$arg" == "$OUTPUT_PATH" ]] && run_seen=1
    previous="$arg"
  done < "/proc/$pid/cmdline"
  [[ "$script_seen" == 1 && "$run_seen" == 1 ]]
}

harbor_online_analysis_pid_matches_run() {
  local pid="$1" arg script_seen=0 run_seen=0
  [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/cmdline" ]] || return 1
  while IFS= read -r -d '' arg; do
    [[ "$arg" == "$SCRIPT_DIR/scripts/online_rule_analyzer.py" ]] && script_seen=1
    [[ "$arg" == "$OUTPUT_PATH" ]] && run_seen=1
  done < "/proc/$pid/cmdline"
  [[ "$script_seen" == 1 && "$run_seen" == 1 ]]
}

# Call only after the subsystem-specific PID identity check succeeds.
harbor_terminate_validated_process() {
  local pid="$1" wait_attempts="${2:-30}" sid signal_target attempt
  sid="$(ps -o sid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
  signal_target="$pid"
  [[ "$sid" == "$pid" ]] && signal_target="-$pid"
  kill -TERM -- "$signal_target" >/dev/null 2>&1 || true
  for attempt in $(seq 1 "$wait_attempts"); do
    kill -0 "$pid" >/dev/null 2>&1 || break
    sleep 1
  done
  if kill -0 "$pid" >/dev/null 2>&1; then
    kill -KILL -- "$signal_target" >/dev/null 2>&1 || true
  fi
}

harbor_stop_online_analysis() {
  [[ -f "$HARBOR_ONLINE_ANALYSIS_PID_FILE" ]] || return 0
  local pid
  pid="$(cat "$HARBOR_ONLINE_ANALYSIS_PID_FILE" 2>/dev/null || true)"
  if [[ ! "$pid" =~ ^[0-9]+$ ]] || ! kill -0 "$pid" >/dev/null 2>&1; then
    rm -f "$HARBOR_ONLINE_ANALYSIS_PID_FILE"
    return 0
  fi
  if ! harbor_online_analysis_pid_matches_run "$pid"; then
    echo "[ERROR] refusing to stop unrelated process from $HARBOR_ONLINE_ANALYSIS_PID_FILE: pid=$pid" >&2
    return 1
  fi
  harbor_terminate_validated_process "$pid"
  rm -f "$HARBOR_ONLINE_ANALYSIS_PID_FILE"
}

harbor_stop_monitor() {
  [[ -f "$HARBOR_MONITOR_PID_FILE" ]] || return 0
  local pid
  pid="$(cat "$HARBOR_MONITOR_PID_FILE" 2>/dev/null || true)"
  if [[ ! "$pid" =~ ^[0-9]+$ ]] || ! kill -0 "$pid" >/dev/null 2>&1; then
    rm -f "$HARBOR_MONITOR_PID_FILE"
    return 0
  fi
  if ! harbor_monitor_pid_matches_run "$pid"; then
    echo "[ERROR] refusing to stop unrelated process from $HARBOR_MONITOR_PID_FILE: pid=$pid" >&2
    return 1
  fi
  harbor_terminate_validated_process "$pid"
  rm -f "$HARBOR_MONITOR_PID_FILE"
}

harbor_reset_run_state() (
  mkdir -p "$OUTPUT_PATH/fixer"
  exec 8>>"$OUTPUT_PATH/fixer/.fixer-control.lock"
  flock -x 8
  if ! python3 "$SCRIPT_DIR/scripts/controller.py" --run-dir "$OUTPUT_PATH" fixer reset-control --lock-fd 8 >/dev/null; then
    echo "[ERROR] refusing to reset run state while Fixer is active" >&2
    return 1
  fi
  harbor_stop_analyzer_supervisor
  harbor_stop_analyzer
  harbor_stop_monitor
  harbor_stop_online_analysis
  rm -f "$QUEUE_DIR"/worker-*.current "$LOCK_FILE" "$WORKERS_READY_FILE" "$WORKERS_FAILED_FILE"
  rm -f "$NEXT_INDEX_FILE"
  rm -f "$OUTPUT_PATH/.monitor_state.json" "$HARBOR_MONITOR_LOG_FILE" "$HARBOR_BENCHMARK_PID_FILE" "$HARBOR_BENCHMARK_EXIT_FILE" "$HARBOR_JOB_DIR_FILE"
  rm -rf "$HARBOR_MONITOR_DIR"
  rm -f "$HARBOR_ANALYZER_PID_FILE" "$HARBOR_ANALYZER_SUPERVISOR_PID_FILE" "$HARBOR_ANALYZER_SUPERVISOR_ID_FILE" "$HARBOR_ANALYZER_LOG_FILE" "$HARBOR_ANALYZER_OUTPUT_DIR/.analyzer_state.json" "$HARBOR_ANALYZER_OUTPUT_DIR/.analyzer-ready" "$HARBOR_ANALYZER_OUTPUT_DIR/analyzer-artifacts-latest.json" "$HARBOR_ANALYZER_OUTPUT_DIR/benchmark-summary.md"
  rm -rf "$HARBOR_ANALYZER_OUTPUT_DIR/benchmark-summary"
  rm -f "$OUTPUT_PATH/fixer/fix-report-latest.md"
  rm -f "$HARBOR_ONLINE_ANALYSIS_PID_FILE" "$HARBOR_ONLINE_ANALYSIS_LOG_FILE"
  rm -f "$HARBOR_ONLINE_ANALYSIS_DIR/environment-events.jsonl" "$HARBOR_ONLINE_ANALYSIS_DIR/environment-summary.json"
  : > "$QUEUE_DIR/done.txt"
  : > "$QUEUE_DIR/failed.txt"
)

harbor_stop_analyzer_supervisor() {
  [[ -f "$HARBOR_ANALYZER_SUPERVISOR_PID_FILE" ]] || return 0
  local pid
  pid="$(cat "$HARBOR_ANALYZER_SUPERVISOR_PID_FILE" 2>/dev/null || true)"
  if [[ ! "$pid" =~ ^[0-9]+$ ]] || ! kill -0 "$pid" >/dev/null 2>&1; then
    rm -f "$HARBOR_ANALYZER_SUPERVISOR_PID_FILE" "$HARBOR_ANALYZER_SUPERVISOR_ID_FILE"
    return 0
  fi
  if ! harbor_analyzer_supervisor_pid_matches_run "$pid"; then
    echo "[ERROR] refusing to stop unrelated process from $HARBOR_ANALYZER_SUPERVISOR_PID_FILE: pid=$pid" >&2
    return 1
  fi
  kill -TERM "$pid" >/dev/null 2>&1 || true
  for _ in $(seq 1 10); do
    kill -0 "$pid" >/dev/null 2>&1 || break
    sleep 1
  done
  if kill -0 "$pid" >/dev/null 2>&1; then
    kill -KILL "$pid" >/dev/null 2>&1 || true
  fi
  rm -f "$HARBOR_ANALYZER_SUPERVISOR_PID_FILE" "$HARBOR_ANALYZER_SUPERVISOR_ID_FILE"
}

harbor_stop_analyzer() {
  [[ -f "$HARBOR_ANALYZER_PID_FILE" ]] || return 0
  local expected_pid="${1:-}" current_pid pid
  current_pid="$(cat "$HARBOR_ANALYZER_PID_FILE" 2>/dev/null || true)"
  pid="${expected_pid:-$current_pid}"
  if [[ -n "$expected_pid" && "$current_pid" != "$expected_pid" ]]; then
    [[ "$expected_pid" =~ ^[0-9]+$ ]] || return 0
  fi
  if [[ ! "$pid" =~ ^[0-9]+$ ]] || ! kill -0 "$pid" >/dev/null 2>&1; then
    [[ -z "$expected_pid" || "$current_pid" == "$expected_pid" ]] && rm -f "$HARBOR_ANALYZER_PID_FILE"
    return 0
  fi
  if ! harbor_analyzer_pid_matches_run "$pid"; then
    echo "[ERROR] refusing to stop unrelated process from $HARBOR_ANALYZER_PID_FILE: pid=$pid" >&2
    return 1
  fi
  harbor_terminate_validated_process "$pid"
  [[ -z "$expected_pid" || "$current_pid" == "$expected_pid" ]] && rm -f "$HARBOR_ANALYZER_PID_FILE"
}
