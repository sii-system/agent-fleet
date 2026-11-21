#!/usr/bin/env bash
set -euo pipefail

# Dataset resolution, task selection, and queue claims.
# Sourced by ../env.sh; SCRIPT_DIR remains the Harbor directory.

harbor_validate_dataset_path_configuration() {
  if [[ "$DATASET_NAME" == "agent-fleet-swe-rebench-v2" ]] &&
     [[ "$_HARBOR_DATASET_PATH_CONFIGURED" != "1" ]]; then
    printf '%s\n' \
      "[ERROR] DATASET_PATH is required for taskset agent-fleet-swe-rebench-v2" \
      "[ERROR] point DATASET_PATH at materialized output from Tasks/SWE-rebench-v2" >&2
    return 1
  fi
}

harbor_generate_task_file() {
  local destination="${1:-$TASK_FILE}" source_file=""
  harbor_validate_dataset_path_configuration || return 1
  if [[ "${HARBOR_CC_WEB_MCP_ENABLED:-0}" == "1" ]]; then
    harbor_prepare_web_search_dataset || return 1
  fi
  # Explicit local paths must be validated against the checkout the user
  # selected, not a similarly named repository manifest.
  if [[ -n "$TASK_SOURCE_FILE" || -z "$FLEET_TASKS" || "$DATASET_NAME" != "auto" ]]; then
    source_file="$(harbor_task_source_file || true)"
  fi
  if [[ -n "$source_file" ]]; then
    cp "$source_file" "$destination"
    return 0
  fi

  if [[ ! -d "$DATASET_PATH" ]]; then
    echo "DATASET_PATH not found: $DATASET_PATH" >&2
    return 1
  fi

  # Harbor local datasets are one task per top-level directory.  SWE-smith uses
  # instruction.md, while SETA/Terminal-Bench tasks use task.yaml.  Keep this
  # scan format-neutral so the same zellij runner can handle both datasets.
  python3 "$SCRIPT_DIR/env.py" generate-task-file "$DATASET_PATH" "$destination"
}

harbor_filter_task_file() {
  local source_file="$1" destination="$2"
  python3 "$SCRIPT_DIR/env.py" filter-task-file "$source_file" "$destination" "$FLEET_TASKS"
}

harbor_validate_local_task_selection() {
  [[ -n "$FLEET_TASKS" ]] || return 0
  local all_tasks selected_tasks
  all_tasks="$(mktemp "${TMPDIR:-/tmp}/harbor-all-tasks.XXXXXX")"
  selected_tasks="$(mktemp "${TMPDIR:-/tmp}/harbor-selected-tasks.XXXXXX")"
  if ! harbor_generate_task_file "$all_tasks" ||
     ! harbor_filter_task_file "$all_tasks" "$selected_tasks"; then
    rm -f "$all_tasks" "$selected_tasks"
    return 2
  fi
  rm -f "$all_tasks" "$selected_tasks"
}

harbor_dataset_name_is_registry_id() {
  local name="$1"
  [[ "$name" == */* || "$name" == *@* ]]
}

harbor_dataset_kind() {
  if [[ "$DATASET_NAME" != "auto" ]]; then
    if harbor_dataset_name_is_registry_id "$DATASET_NAME"; then
      printf 'harbor\n'
      return 0
    fi
    printf '%s\n' "$DATASET_NAME"
    return 0
  fi
  case "$DATASET_PATH" in
    */seta-env|*/seta-env/Dataset|*seta*) printf 'seta\n' ;;
    *swesmith*|*smith*) printf 'smith\n' ;;
    *terminal-bench-2-1*|*terminalbench21*|*terminal-bench21*) printf 'terminalbench21\n' ;;
    *swebench-verified*|*sweverify*|*swe-verify*) printf 'sweverify\n' ;;
    *) printf 'harbor\n' ;;
  esac
}

harbor_builtin_task_file() {
  case "$(harbor_dataset_kind)" in
    seta) printf '%s\n' "$TASKS_DIR/SETA/harbor_tasks.txt" ;;
    smith) printf '%s\n' "$TASKS_DIR/SWE-smith/harbor_tasks.txt" ;;
    terminalbench21) printf '%s\n' "$TASKS_DIR/Terminal-bench-2/harbor_terminalbench21_tasks.txt" ;;
    sweverify) printf '%s\n' "$TASKS_DIR/SWE-verify/harbor_tasks.txt" ;;
    *) return 1 ;;
  esac
}

harbor_task_source_file() {
  if [[ -n "${TASK_SOURCE_FILE:-}" ]]; then
    if [[ ! -s "$TASK_SOURCE_FILE" ]]; then
      echo "TASK_SOURCE_FILE not found or empty: $TASK_SOURCE_FILE" >&2
      return 1
    fi
    printf '%s\n' "$TASK_SOURCE_FILE"
    return 0
  fi

  local builtin
  builtin="$(harbor_builtin_task_file || true)"
  if [[ -n "$builtin" && -s "$builtin" ]]; then
    printf '%s\n' "$builtin"
    return 0
  fi

  if [[ -n "$builtin" ]]; then
    echo "[WARN] built-in task list missing or empty: $builtin; falling back to DATASET_PATH scan" >&2
  fi
  return 1
}

harbor_metric_mode() {
  if [[ "$METRIC_MODE" != "auto" ]]; then
    printf '%s\n' "$METRIC_MODE"
    return 0
  fi
  if [[ "$(harbor_dataset_kind)" == "seta" || "$(harbor_dataset_kind)" == "terminalbench21" || "$(harbor_dataset_kind)" == "sweverify" ]]; then
    printf 'success\n'
  else
    printf 'reward\n'
  fi
}

harbor_registry_dataset_name() {
  case "$DATASET_NAME" in
    seta) printf 'seta-env\n'; return 0 ;;
    terminalbench21) printf '%s\n' "$HARBOR_TERMINALBENCH21_REGISTRY_ID"; return 0 ;;
    sweverify) printf 'swebench-verified\n'; return 0 ;;
  esac
  if harbor_dataset_name_is_registry_id "$DATASET_NAME"; then
    printf '%s\n' "$DATASET_NAME"
    return 0
  fi
  return 1
}

harbor_metadata_dataset_name() {
  if harbor_uses_registry_dataset; then
    harbor_registry_dataset_name
  else
    harbor_dataset_kind
  fi
}

harbor_uses_registry_dataset() {
  harbor_registry_dataset_name >/dev/null
}

harbor_uses_native_runner() {
  [[ "$HARBOR_NATIVE_CONCURRENCY" == "1" ]] || harbor_uses_registry_dataset
}

harbor_registry_task_name() {
  local task_name="$1"
  if [[ "$(harbor_registry_dataset_name 2>/dev/null || true)" == "$HARBOR_TERMINALBENCH21_REGISTRY_ID" ]] \
    && [[ "$task_name" != */* ]]; then
    printf 'terminal-bench/%s\n' "$task_name"
    return 0
  fi
  printf '%s\n' "$task_name"
}

harbor_prepare_registry_task_selection() {
  [[ -n "$FLEET_TASKS" ]] || return 0
  local source_file selected_tasks
  source_file="$(harbor_task_source_file || true)"
  if [[ -z "$source_file" || ! -s "$source_file" ]]; then
    printf '[ERROR] --task is unsupported for Harbor registry taskset: %s\n' "$DATASET_NAME" >&2
    return 2
  fi
  selected_tasks="$(mktemp "${TMPDIR:-/tmp}/harbor-selected-tasks.XXXXXX")"
  if ! harbor_filter_task_file "$source_file" "$selected_tasks"; then
    rm -f "$selected_tasks"
    return 2
  fi
  rm -f "$selected_tasks"
  HARBOR_INCLUDE_TASKS="$FLEET_TASKS"
  export HARBOR_INCLUDE_TASKS
}

harbor_validate_task_selection() {
  [[ -n "$FLEET_TASKS" ]] || return 0
  if [[ "$ROLLOUT" == "1" ]]; then
    printf '[ERROR] --task is unsupported when ROLLOUT=1\n' >&2
    return 2
  fi
  if harbor_uses_registry_dataset; then
    harbor_prepare_registry_task_selection
  else
    harbor_validate_local_task_selection
  fi
}

harbor_prepare_task_file() {
  mkdir -p "$(dirname "$TASK_FILE")"
  (
  flock -x 9
  if [[ -z "$FLEET_TASKS" ]]; then
    if [[ "${RESET_RUN:-0}" == "1" || ! -s "$TASK_FILE" ]]; then
      harbor_generate_task_file
    fi
  else
    local all_tasks selected_tasks
    all_tasks="$(mktemp "$(dirname "$TASK_FILE")/.all-tasks.XXXXXX")"
    selected_tasks="$(mktemp "$(dirname "$TASK_FILE")/.selected-tasks.XXXXXX")"
    if ! harbor_generate_task_file "$all_tasks" ||
       ! harbor_filter_task_file "$all_tasks" "$selected_tasks"; then
      rm -f "$all_tasks" "$selected_tasks"
      return 2
    fi
    rm -f "$all_tasks"

    if [[ "${RESET_RUN:-0}" != "1" && -s "$TASK_FILE" ]]; then
      if ! cmp -s "$TASK_FILE" "$selected_tasks"; then
        rm -f "$selected_tasks"
        printf '[ERROR] task selection does not match existing task file: %s\n' "$TASK_FILE" >&2
        printf '[ERROR] set RESET_RUN=1 or use a new RUN_ID\n' >&2
        return 2
      fi
      rm -f "$selected_tasks"
    else
      mv -f "$selected_tasks" "$TASK_FILE"
    fi
  fi
  if [[ ! -f "$NEXT_INDEX_FILE" ]]; then
    echo 1 > "$NEXT_INDEX_FILE"
  fi
  ) 9>"${TASK_FILE}.lock"
}

harbor_task_count() {
  if [[ -f "$TASK_FILE" ]]; then
    awk 'NF {n++} END {print n+0}' "$TASK_FILE"
  else
    echo 0
  fi
}

harbor_task_line_count() {
  if [[ -f "$TASK_FILE" ]]; then
    wc -l < "$TASK_FILE" | tr -d ' '
  else
    echo 0
  fi
}

harbor_pid_is_alive() {
  local pid="$1" err
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  if err="$(kill -0 -- "$pid" 2>&1)"; then
    return 0
  fi
  [[ "$err" == *"not permitted"* ]]
}

harbor_queue_claim_attempts() {
  local n="${HARBOR_QUEUE_CLAIM_ATTEMPTS:-2}"
  [[ "$n" =~ ^[0-9]+$ && "$n" -ge 1 ]] || n=2
  printf '%s\n' "$n"
}

harbor_claim_dir() {
  printf '%s\n' "$QUEUE_DIR/claims"
}

harbor_task_index_recorded() {
  local index="$1" file
  for file in "$QUEUE_DIR/done.txt" "$QUEUE_DIR/failed.txt"; do
    [[ -f "$file" ]] || continue
    if awk -F '\t' -v idx="$index" 'NF && $1 == idx { found=1 } END { exit found ? 0 : 1 }' "$file"; then
      return 0
    fi
  done
  return 1
}

harbor_write_claim() {
  local index="$1" task_name="$2" attempt="$3"
  mkdir -p "$(harbor_claim_dir)"
  printf '%s\t%s\t%s\t%s\n' "$index" "$task_name" "$$" "$attempt" > "$(harbor_claim_dir)/$index"
}

harbor_release_claim() {
  local index="$1" claim pid
  [[ "$index" =~ ^[0-9]+$ ]] || return 0
  mkdir -p "$QUEUE_DIR"
  exec 9>"$LOCK_FILE"
  flock 9
  claim="$(harbor_claim_dir)/$index"
  if [[ -f "$claim" ]]; then
    pid="$(awk -F '\t' 'NR==1 { print $3 }' "$claim")"
    if [[ "$pid" == "$$" ]]; then
      rm -f "$claim"
    fi
  fi
  flock -u 9
}

harbor_pop_reclaim_locked() {
  local file="$QUEUE_DIR/reclaim.txt" line lines
  [[ -s "$file" ]] || return 1
  line="$(head -n 1 "$file" | tr -d '\r')"
  lines="$(wc -l < "$file" | tr -d ' ')"
  if [[ "$lines" -le 1 ]]; then
    : > "$file"
  else
    tail -n +2 "$file" > "${file}.tmp"
    mv -f "${file}.tmp" "$file"
  fi
  [[ -n "$line" ]] || return 1
  printf '%s\n' "$line"
}

harbor_append_reclaim_locked() {
  local index="$1" task_name="$2" attempt="$3" file="$QUEUE_DIR/reclaim.txt"
  if [[ -f "$file" ]] && awk -F '\t' -v idx="$index" 'NF && $1 == idx { found=1 } END { exit found ? 0 : 1 }' "$file"; then
    return 0
  fi
  printf '%s\t%s\t%s\n' "$index" "$task_name" "$attempt" >> "$file"
}

harbor_fail_lost_claim_locked() {
  local index="$1" task_name="$2"
  if harbor_task_index_recorded "$index"; then
    return 0
  fi
  printf '%s\t%s\t%s\t%s\n' "$index" "$task_name" "worker-lost" "WorkerLost" >> "$QUEUE_DIR/failed.txt"
}

# Caller holds the queue lock. Dead claims go back on the reclaim list until
# HARBOR_QUEUE_CLAIM_ATTEMPTS is exhausted, then they are recorded as failed.
harbor_recover_lost_claims_locked() {
  local claim index task_name pid attempt max_attempts claims_dir current current_pid
  max_attempts="$(harbor_queue_claim_attempts)"
  claims_dir="$(harbor_claim_dir)"
  mkdir -p "$claims_dir"
  touch "$QUEUE_DIR/done.txt" "$QUEUE_DIR/failed.txt"
  for claim in "$claims_dir"/*; do
    [[ -f "$claim" ]] || continue
    index=""
    task_name=""
    pid=""
    attempt=""
    IFS=$'\t' read -r index task_name pid attempt < "$claim" || true
    [[ "$index" =~ ^[0-9]+$ && -n "$task_name" ]] || {
      rm -f "$claim"
      continue
    }
    [[ "$attempt" =~ ^[0-9]+$ ]] || attempt=1
    if harbor_pid_is_alive "$pid"; then
      continue
    fi
    if harbor_task_index_recorded "$index"; then
      rm -f "$claim"
      continue
    fi
    if [[ "$attempt" -ge "$max_attempts" ]]; then
      harbor_fail_lost_claim_locked "$index" "$task_name"
    else
      harbor_append_reclaim_locked "$index" "$task_name" "$attempt"
    fi
    rm -f "$claim"
  done
  for current in "$QUEUE_DIR"/worker-*.current; do
    [[ -f "$current" ]] || continue
    current_pid="$(awk -F '\t' 'NR==1 { print $3 }' "$current")"
    if [[ -z "$current_pid" ]]; then
      continue
    fi
    if ! harbor_pid_is_alive "$current_pid"; then
      rm -f "$current"
    fi
  done
}

harbor_recover_lost_claims() {
  mkdir -p "$QUEUE_DIR"
  exec 9>"$LOCK_FILE"
  flock 9
  harbor_recover_lost_claims_locked
  flock -u 9
}

harbor_count_live_workers() {
  local current pid n=0
  for current in "$QUEUE_DIR"/worker-*.current; do
    [[ -f "$current" ]] || continue
    pid="$(awk -F '\t' 'NR==1 { print $3 }' "$current")"
    if [[ -z "$pid" ]]; then
      n=$((n + 1))
      continue
    fi
    if harbor_pid_is_alive "$pid"; then
      n=$((n + 1))
    fi
  done
  printf '%s\n' "$n"
}

harbor_fail_pending_reclaim_locked() {
  local file="$QUEUE_DIR/reclaim.txt" line index task_name
  [[ -s "$file" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -n "$line" ]] || continue
    index="$(printf '%s' "$line" | cut -f1)"
    task_name="$(printf '%s' "$line" | cut -f2)"
    [[ "$index" =~ ^[0-9]+$ && -n "$task_name" ]] || continue
    harbor_fail_lost_claim_locked "$index" "$task_name"
  done < "$file"
  : > "$file"
}

harbor_ensure_queue_recovery_worker() {
  local pidfile old cmd count_file count budget
  [[ -s "$QUEUE_DIR/reclaim.txt" ]] || return 0
  [[ "$(harbor_count_live_workers)" == "0" ]] || return 0
  pidfile="$QUEUE_DIR/recovery.pid"
  if [[ -f "$pidfile" ]]; then
    old="$(tr -d '[:space:]' < "$pidfile" || true)"
    if harbor_pid_is_alive "$old"; then
      return 0
    fi
  fi
  count_file="$QUEUE_DIR/recovery.count"
  count=0
  if [[ -f "$count_file" ]]; then
    count="$(tr -d '[:space:]' < "$count_file" || true)"
  fi
  [[ "$count" =~ ^[0-9]+$ ]] || count=0
  budget="${HARBOR_QUEUE_RECOVERY_SPAWNS:-3}"
  [[ "$budget" =~ ^[0-9]+$ && "$budget" -ge 1 ]] || budget=3
  if [[ "$count" -ge "$budget" ]]; then
    mkdir -p "$QUEUE_DIR"
    exec 9>"$LOCK_FILE"
    flock 9
    harbor_fail_pending_reclaim_locked
    flock -u 9
    return 0
  fi
  cmd="${HARBOR_QUEUE_RECOVERY_COMMAND:-$SCRIPT_DIR/run_harbor_worker.sh}"
  mkdir -p "$RUNTIME_DIR/worker-logs"
  bash "$cmd" recovery >> "$RUNTIME_DIR/worker-logs/worker-recovery.log" 2>&1 &
  printf '%s\n' "$!" > "$pidfile"
  printf '%s\n' "$((count + 1))" > "$count_file"
}

harbor_ensure_dataset() {
  local dataset_kind
  harbor_validate_dataset_path_configuration || return 1
  if [[ "${HARBOR_CC_WEB_MCP_ENABLED:-0}" == "1" ]]; then
    harbor_prepare_web_search_dataset || return 1
  fi
  dataset_kind="$(harbor_dataset_kind)"

  if harbor_uses_registry_dataset; then
    harbor_registry_dataset_name >/dev/null
    return 0
  fi

  if [[ -d "$DATASET_PATH" ]] && [[ -n "$(find -L "$DATASET_PATH" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | head -n 1)" ]]; then
    return 0
  fi

  if [[ "$dataset_kind" == "smith" && "$SMITH_GENERATE_IF_MISSING" == "1" ]]; then
    if [[ ! -d "$SMITH_ADAPTER_DIR" ]]; then
      echo "smith dataset missing and adapter not found: $SMITH_ADAPTER_DIR" >&2
      return 1
    fi
    echo "[INFO] smith dataset not found at $DATASET_PATH, generating with $SMITH_ADAPTER_DIR"
    (
      cd "$SMITH_ADAPTER_DIR"
      uv sync
      uv run run_adapter.py --limit 0
    )
  fi

  if [[ ! -d "$DATASET_PATH" ]]; then
    echo "DATASET_PATH not found: $DATASET_PATH" >&2
    if [[ "$dataset_kind" == "harbor" ]]; then
      echo "automatic TerminalBench dataset cloning was removed" >&2
      printf '%s\n' \
        "set DATASET_PATH to an existing local Harbor dataset" \
        "or select a registry DATASET_NAME" >&2
    fi
    return 1
  fi
}

harbor_pick_task() {
  exec 9>"$LOCK_FILE"
  flock 9
  mkdir -p "$QUEUE_DIR"
  touch "$QUEUE_DIR/done.txt" "$QUEUE_DIR/failed.txt"
  harbor_recover_lost_claims_locked

  local line index task_name attempt idx total_lines
  line="$(harbor_pop_reclaim_locked || true)"
  if [[ -n "$line" ]]; then
    index="$(printf '%s' "$line" | cut -f1)"
    task_name="$(printf '%s' "$line" | cut -f2)"
    attempt="$(printf '%s' "$line" | cut -f3)"
    [[ "$attempt" =~ ^[0-9]+$ ]] || attempt=1
    harbor_write_claim "$index" "$task_name" "$((attempt + 1))"
    flock -u 9
    printf '%s\t%s\n' "$index" "$task_name"
    return 0
  fi

  total_lines="$(harbor_task_line_count)"
  idx="$(cat "$NEXT_INDEX_FILE" 2>/dev/null || echo 1)"
  [[ "$idx" =~ ^[0-9]+$ ]] || idx=1
  while [[ "$idx" -le "$total_lines" ]]; do
    task_name="$(sed -n "${idx}p" "$TASK_FILE" | tr -d '\r')"
    echo $((idx + 1)) > "$NEXT_INDEX_FILE"
    if [[ -z "$task_name" ]]; then
      idx=$((idx + 1))
      continue
    fi
    harbor_write_claim "$idx" "$task_name" 1
    flock -u 9
    printf '%s\t%s\n' "$idx" "$task_name"
    return 0
  done
  flock -u 9
  return 1
}
