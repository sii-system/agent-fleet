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
  python3 - "$QUEUE_DIR/done.txt" <<'PY'
import collections
import sys

path = sys.argv[1]
counter = collections.Counter()
total = 0
try:
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            reward = parts[2] if parts[2] else "none"
            counter[reward] += 1
            total += 1
except FileNotFoundError:
    pass

if not total:
    print("(none)")
else:
    for reward, count in sorted(counter.items(), key=lambda item: (item[0] != "1.0", item[0])):
        print(f"reward={reward}: {count}")
PY
}

success_stats() {
  python3 - "$QUEUE_DIR/done.txt" "$QUEUE_DIR/failed.txt" <<'PY'
import sys

done_path, failed_path = sys.argv[1:3]
done = 0
success = 0
try:
    with open(done_path, encoding="utf-8") as handle:
        for line in handle:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            done += 1
            value = (parts[2] or "").strip().lower()
            if value in {"1", "1.0", "true", "success", "resolved", "pass", "passed"}:
                success += 1
except FileNotFoundError:
    pass

failed = 0
try:
    with open(failed_path, encoding="utf-8") as handle:
        failed = sum(1 for line in handle if line.strip())
except FileNotFoundError:
    pass

finished = done + failed
fail = finished - success
rate = (success / finished * 100.0) if finished else 0.0
print(f"success:      {success}")
print(f"fail:         {fail}")
print(f"success_rate: {rate:.2f}%")
PY
}

exception_stats() {
  python3 - "$QUEUE_DIR/done.txt" "$QUEUE_DIR/failed.txt" <<'PY'
import collections
import sys

counter = collections.Counter()
for path in sys.argv[1:]:
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 4 and parts[3]:
                    counter[parts[3]] += 1
                elif path.endswith("failed.txt"):
                    counter["missing_result"] += 1
    except FileNotFoundError:
        pass

if not counter:
    print("(none)")
else:
    for name, count in counter.most_common(10):
        print(f"{name}: {count}")
PY
}

environment_signal_stats() {
  python3 - "$HARBOR_ONLINE_ANALYSIS_DIR/environment-summary.json" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        summary = json.load(handle)
except (FileNotFoundError, json.JSONDecodeError):
    print("(none)")
    raise SystemExit(0)

counter = summary.get("monitor_environment_events_by_type") or {}
if not counter:
    print("(none)")
else:
    for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:10]:
        print(f"{name}: {count}")
PY
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
  else
    echo "OPENCODE_VERSION: $OPENCODE_VERSION"
    echo "MODEL:       $HARBOR_MODEL"
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
