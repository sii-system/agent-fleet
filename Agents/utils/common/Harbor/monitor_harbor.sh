#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/env.sh"
harbor_validate_agent

harbor_init_run_dirs
harbor_ensure_dataset
harbor_prepare_task_file

if [[ ! -s "$TASK_FILE" ]]; then
  echo "no tasks found (AGENT=$AGENT, DATASET_PATH=$DATASET_PATH)" >&2
  touch "$WORKERS_FAILED_FILE"
  exit 1
fi

if ! harbor_prepare_agent_runtime; then
  exit 1
fi

count_done() {
  awk 'NF {n++} END {print n+0}' "$QUEUE_DIR/done.txt" 2>/dev/null || echo 0
}

count_failed() {
  awk 'NF {n++} END {print n+0}' "$QUEUE_DIR/failed.txt" 2>/dev/null || echo 0
}

next_index() {
  cat "$NEXT_INDEX_FILE" 2>/dev/null || echo 1
}

reward_stats() {
  python3 "$SCRIPT_DIR/harbor_monitor_utils.py" rewards "$QUEUE_DIR/done.txt"
}

success_stats() {
  python3 "$SCRIPT_DIR/harbor_monitor_utils.py" success \
    "$QUEUE_DIR/done.txt" "$QUEUE_DIR/failed.txt"
}

exception_stats() {
  python3 "$SCRIPT_DIR/harbor_monitor_utils.py" exceptions \
    "$QUEUE_DIR/done.txt" "$QUEUE_DIR/failed.txt"
}

environment_signal_stats() {
  python3 "$SCRIPT_DIR/harbor_monitor_utils.py" environment-signals \
    "$HARBOR_ONLINE_ANALYSIS_DIR/environment-summary.json"
}

SUMMARY_FILE="$OUTPUT_PATH/summary.txt"

collect_counts() {
  total="$(harbor_task_count)"
  next="$(next_index)"
  done_n="$(count_done)"
  failed_n="$(count_failed)"
  running_n="$(find "$QUEUE_DIR" -maxdepth 1 -name 'worker-*.current' | wc -l | tr -d ' ')"
  claimed=$((next - 1))
  if [[ "$claimed" -lt 0 ]]; then
    claimed=0
  fi
  remaining=$((total - claimed))
  if [[ "$remaining" -lt 0 ]]; then
    remaining=0
  fi
}

all_tasks_finished() {
  [[ "$total" -gt 0 ]] || return 1
  [[ "$running_n" -eq 0 ]] || return 1
  [[ $((done_n + failed_n)) -ge "$total" ]] || return 1
}

render_report() {
  local prep_status
  echo "RUN_ID:      $RUN_ID"
  echo "AGENT:       $AGENT"
  echo "DATASET_NAME: $(harbor_dataset_kind)"
  echo "DATASET:     $DATASET_PATH"
  echo "MODEL:       $HARBOR_MODEL"
  echo "OUTPUT_PATH: $OUTPUT_PATH"
  echo "TASK_FILE:   $TASK_FILE"
  echo "QUEUE_DIR:   $QUEUE_DIR"
  echo "OPIK_URL:    $OPIK_URL_OVERRIDE"
  echo "OPIK_PROJECT_NAME: $OPIK_PROJECT_NAME"
  if harbor_agent_is_claude_code; then
    echo "CLAUDE_CODE_VERSION: $CLAUDE_CODE_VERSION"
    prep_status="unknown"
    [[ -f "$RUNTIME_DIR/local-deps-prepare.status" ]] && prep_status="$(cat "$RUNTIME_DIR/local-deps-prepare.status" 2>/dev/null || true)"
    echo "LOCAL_DEPS_PREP: $prep_status"
    echo "LOCAL_WHEEL_URL: ${HARBOR_LOCAL_WHEEL_SERVER_URL:-<none>}"
    echo "LOCAL_WHEEL_LOG: $LOCAL_DEPS_LOG_FILE"
  elif harbor_agent_is_opencode; then
    echo "OPENCODE_VERSION: $OPENCODE_VERSION"
    echo "MODEL:       $HARBOR_MODEL"
    prep_status="unknown"
    [[ -f "$HARBOR_RUNNER_PREPARE_STATUS_FILE" ]] && prep_status="$(cat "$HARBOR_RUNNER_PREPARE_STATUS_FILE" 2>/dev/null || true)"
    echo "RUNNER_CLI_PREP: $prep_status"
    echo "RUNNER_CLI_LOG:  $HARBOR_RUNNER_PREPARE_LOG_FILE"
  elif harbor_agent_is_pi; then
    echo "PI_VERSION:   $PI_VERSION"
    echo "PI_THINKING:  $PI_THINKING_LEVEL"
    echo "PI_EXTENSION_DIR: ${PI_EXTENSION_DIR:-<none>}"
    if [[ -n "${PI_EXTENSION_SOURCE:-}" && -d "$PI_EXTENSION_SOURCE" ]]; then
      local plugin_file ext_count=0
      for plugin_file in "$PI_EXTENSION_SOURCE"/*.ts; do
        [[ -e "$plugin_file" ]] || continue
        echo "  EXT: $(basename "$plugin_file")"
        ext_count=$((ext_count + 1))
      done
      if [[ "$ext_count" -eq 0 ]]; then
        echo "PI_EXTENSIONS: <none>"
      else
        echo "PI_EXTENSION_MOUNT: $PI_EXTENSION_DIR"
      fi
    else
      echo "PI_EXTENSIONS: <none>"
    fi
    echo "MODEL:        $HARBOR_MODEL"
    prep_status="unknown"
    [[ -f "$RUNTIME_DIR/local-deps-prepare.status" ]] && prep_status="$(cat "$RUNTIME_DIR/local-deps-prepare.status" 2>/dev/null || true)"
    echo "LOCAL_DEPS_PREP: $prep_status"
    echo "LOCAL_WHEEL_URL: ${HARBOR_LOCAL_WHEEL_SERVER_URL:-<none>}"
    echo "LOCAL_WHEEL_LOG: $LOCAL_DEPS_LOG_FILE"
  elif harbor_agent_is_dsh; then
    local prep_status="unknown"
    [[ ! -f "$RUNTIME_DIR/local-deps-prepare.status" ]] \
      || prep_status="$(cat "$RUNTIME_DIR/local-deps-prepare.status" 2>/dev/null || true)"
    echo "DSH_PROFILE:    sdk-minimal"
    echo "DSH_VERSION:    $DSH_SDK_MINIMAL_CLI_VERSION"
    echo "DSH_SDK_SOURCE: $DSH_SDK_MINIMAL_SOURCE_REF@$DSH_SDK_MINIMAL_SOURCE_SHA"
    echo "MODEL:          $HARBOR_MODEL"
    echo "LOCAL_DEPS_PREP: $prep_status"
    echo "LOCAL_WHEEL_URL: ${HARBOR_LOCAL_WHEEL_SERVER_URL:-<none>}"
    echo "LOCAL_WHEEL_LOG: $LOCAL_DEPS_LOG_FILE"
  else
    # oracle and any non-pi agent keep the previous opencode-style summary:
    # the runner-CLI prep state and log are the only diagnostics available.
    echo "OPENCODE_VERSION: $OPENCODE_VERSION"
    prep_status="unknown"
    [[ -f "$HARBOR_RUNNER_PREPARE_STATUS_FILE" ]] && prep_status="$(cat "$HARBOR_RUNNER_PREPARE_STATUS_FILE" 2>/dev/null || true)"
    echo "RUNNER_CLI_PREP: $prep_status"
    echo "RUNNER_CLI_LOG:  $HARBOR_RUNNER_PREPARE_LOG_FILE"
  fi
  echo
  echo "total:      $total"
  echo "claimed:    $claimed"
  echo "remaining:  $remaining"
  echo "running:    $running_n"
  echo "done:       $done_n"
  echo "failed:     $failed_n"
  echo
  metric_mode="$(harbor_metric_mode)"
  if [[ "$metric_mode" == "success" ]]; then
    echo "success stats:"
    success_stats
  else
    echo "reward stats:"
    reward_stats
  fi
  echo
  echo "exception stats:"
  exception_stats
  if [[ "$HARBOR_ONLINE_ANALYSIS" == "1" ]]; then
    echo
    echo "environment signal stats:"
    environment_signal_stats
  fi
  echo
  echo "active workers:"

  local found_any=0
  local col=0
  local f worker_id current current_idx item
  while IFS= read -r f; do
    [[ -e "$f" ]] || continue
    found_any=1
    worker_id="$(basename "$f" .current | sed 's/^worker-//')"
    current="$(cat "$f" 2>/dev/null || true)"
    current_idx="$(printf '%s' "$current" | cut -f1)"
    # Keep active workers dense enough for zellij/web panes.
    item="$(printf 'w%s #%s' "$worker_id" "$current_idx")"
    printf '%-14.14s' "$item"
    col=$((col + 1))
    if [[ $((col % 6)) -eq 0 ]]; then
      printf '\n'
    fi
  done < <(find "$QUEUE_DIR" -maxdepth 1 -name 'worker-*.current' | sort -V)

  if [[ $found_any -eq 0 ]]; then
    echo "(none)"
  elif [[ $((col % 6)) -ne 0 ]]; then
    printf '\n'
  fi
}

write_summary() {
  local tmp_file="${SUMMARY_FILE}.tmp"
  {
    echo "finished_at: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    render_report
    echo
    echo "result paths:"
    echo "  output:          $OUTPUT_PATH"
    echo "  done:            $QUEUE_DIR/done.txt"
    echo "  failed:          $QUEUE_DIR/failed.txt"
    echo "  worker logs:     $RUNTIME_DIR/worker-logs"
    if [[ "$HARBOR_ONLINE_ANALYSIS" == "1" ]]; then
      echo "  online analysis: $HARBOR_ONLINE_ANALYSIS_DIR"
    fi
  } > "$tmp_file"
  mv -f "$tmp_file" "$SUMMARY_FILE"
}

while true; do
  collect_counts

  # Detached zellij panes may not have TERM set; do not let clear kill monitor.
  clear 2>/dev/null || printf '\033[H\033[2J'
  render_report

  if all_tasks_finished; then
    write_summary
    echo
    echo "all tasks finished; summary saved to $SUMMARY_FILE"
    if ! harbor_stop_online_analysis; then
      echo "[WARN] failed to stop online analyzer for $OUTPUT_PATH" >&2
    fi
    if [[ "$HARBOR_ZELLIJ_CLOSE_ON_COMPLETE" == "1" ]]; then
      exit 0
    fi
    echo "HARBOR_ZELLIJ_CLOSE_ON_COMPLETE=0; keeping final monitor pane open"
    while true; do
      sleep 3600
    done
  fi

  sleep 2
done
