#!/usr/bin/env bash
set -euo pipefail

RL_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARBOR_SCRIPT_DIR="${HARBOR_SCRIPT_DIR:-$(cd "$RL_SCRIPT_DIR/../common/Harbor" && pwd)}"
. "$HARBOR_SCRIPT_DIR/env.sh"

mkdir -p "$RUNTIME_DIR" "$RL_ACTIVE_DIR" "$RL_QUEUE_DIR/pending" "$RL_QUEUE_DIR/results"

count_files() {
  local dir="$1"
  find "$dir" -maxdepth 1 -name '*.json' 2>/dev/null | wc -l | tr -d ' '
}

dataset_task_count() {
  local worklist
  worklist="$(find "$RL_QUEUE_DIR/worklists" -maxdepth 1 -type f -name '*.txt' 2>/dev/null | head -n 1 || true)"
  if [[ -n "$worklist" && -s "$worklist" ]]; then
    awk 'NF {n++} END {print n+0}' "$worklist"
  elif [[ -d "$RL_DATASET_ROOT" ]]; then
    find "$RL_DATASET_ROOT" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' '
  else
    echo 0
  fi
}

result_stats() {
  python3 "$RL_SCRIPT_DIR/rollout_worker_utils.py" result-stats \
    "$RL_QUEUE_DIR/results"
}

active_workers() {
  local found_any=0
  while IFS= read -r f; do
    [[ -e "$f" ]] || continue
    found_any=1
    worker_id="$(basename "$f" .current | sed 's/^worker-//')"
    current="$(cat "$f" 2>/dev/null || true)"
    request_id="$(printf '%s' "$current" | cut -f1)"
    task_name="$(printf '%s' "$current" | cut -f2)"
    display_name="$(printf '%s' "$current" | cut -f3)"
    polar_task_id="$(printf '%s' "$current" | cut -f5)"
    if [[ -z "$display_name" ]]; then
      display_name="$task_name"
    fi
    polar_short=""
    if [[ -n "$polar_task_id" ]]; then
      polar_short="${polar_task_id: -6}"
    fi
    printf 'worker-%s  task=%s' "$worker_id" "$display_name"
    if [[ -n "$polar_short" ]]; then
      printf '  polar=%s' "$polar_short"
    fi
    if [[ -n "$request_id" ]]; then
      printf '  request=%s' "${request_id:0:12}"
    fi
    printf '\n'
  done < <(find "$RL_ACTIVE_DIR" -maxdepth 1 -name 'worker-*.current' | sort -V)

  if [[ $found_any -eq 0 ]]; then
    echo "(none)"
  fi
}

recent_results() {
  python3 "$RL_SCRIPT_DIR/rollout_worker_utils.py" recent-results \
    "$RL_QUEUE_DIR/results"
}

while true; do
  # Detached zellij panes may not have TERM set; do not let clear kill monitor.
  clear 2>/dev/null || printf '\033[H\033[2J'

  pending_n="$(count_files "$RL_QUEUE_DIR/pending")"
  active_n="$(count_files "$RL_ACTIVE_DIR")"
  result_n="$(count_files "$RL_QUEUE_DIR/results")"
  dataset_total="$(dataset_task_count)"

  total_requests=$((pending_n + active_n + result_n))

  echo "RL rollout Harbor"
  echo "RUN_ID:      $RUN_ID"
  echo "AGENT:       $RL_AGENT"
  echo "MODEL:       $RL_MODEL_NAME"
  echo "DATASET:     $RL_DATASET_NAME -> $RL_DATASET_ROOT"
  echo "RAY_SUBMISSION: ${RL_ZELLIJ_SUBMISSION_ID:-all}"
  echo "POLAR_PORT:  $RL_PORT"
  echo "WORKERS:     $RL_WORKERS"
  echo "OPIK_URL:    $OPIK_URL_OVERRIDE"
  echo "OPIK_PROJECT_NAME: $OPIK_PROJECT_NAME"
  echo
  # Rollout receives tasks from Polar dynamically, so the fixed dataset size is
  # only context; the request counters below are the live job progress.
  echo "dataset_tasks:  $dataset_total"
  echo "job_requests:   $total_requests"
  echo "queued:         $pending_n"
  echo "running:        $active_n"
  echo "finished:       $result_n"
  echo
  result_stats
  echo
  echo "active workers:"
  active_workers
  echo
  echo "recent results:"
  recent_results

  sleep 2
done
