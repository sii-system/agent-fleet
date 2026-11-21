#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARBOR_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT

mkdir -p "$TEST_ROOT/home" "$TEST_ROOT/dataset/alpha" "$TEST_ROOT/dataset/beta"
printf 'version: 1\n' > "$TEST_ROOT/dataset/alpha/task.yaml"
printf 'version: 1\n' > "$TEST_ROOT/dataset/beta/task.yaml"

output="$(
  env \
    HOME="$TEST_ROOT/home" \
    TEST_ROOT="$TEST_ROOT" \
    AGENT="claude-code" \
    DATASET_NAME="auto" \
    DATASET_PATH="$TEST_ROOT/dataset" \
    OUTPUT_PATH="$TEST_ROOT/run" \
    OPIK_URL="" \
    HARBOR_QUEUE_CLAIM_ATTEMPTS="2" \
    bash -c '
      set -euo pipefail
      source "$1"
      mkdir -p "$QUEUE_DIR" "$RUNTIME_DIR/worker-logs"
      touch "$QUEUE_DIR/done.txt" "$QUEUE_DIR/failed.txt"

      fail() {
        printf "FAIL: %s\n" "$1" >&2
        exit 1
      }

      dead_pid() {
        sleep 30 &
        local pid="$!"
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
        printf "%s\n" "$pid"
      }

      reset_queue() {
        rm -rf "$QUEUE_DIR/claims"
        rm -f "$QUEUE_DIR/reclaim.txt" "$QUEUE_DIR/recovery.pid" "$QUEUE_DIR/recovery.count" "$QUEUE_DIR"/worker-*.current
        : > "$QUEUE_DIR/done.txt"
        : > "$QUEUE_DIR/failed.txt"
        echo 1 > "$NEXT_INDEX_FILE"
      }

      reset_queue
      printf "alpha\n\nbeta\n" > "$TASK_FILE"
      first="$(harbor_pick_task || true)"
      second="$(harbor_pick_task || true)"
      third="$(harbor_pick_task || true)"
      [[ "$first" == $'"'"'1\talpha'"'"' ]] || fail "blank line ended the queue before alpha: [$first]"
      [[ "$second" == $'"'"'3\tbeta'"'"' ]] || fail "blank line was treated as the end of the queue: [$second]"
      [[ -z "$third" ]] || fail "expected the queue to end after beta, got [$third]"
      [[ "$(harbor_task_count)" == "2" ]] || fail "blank lines counted as tasks: $(harbor_task_count)"

      reset_queue
      printf "alpha\nbeta\n" > "$TASK_FILE"
      gone="$(dead_pid)"
      mkdir -p "$QUEUE_DIR/claims"
      printf "1\talpha\t%s\t1\n" "$gone" > "$QUEUE_DIR/claims/1"
      echo 3 > "$NEXT_INDEX_FILE"
      reclaimed="$(harbor_pick_task || true)"
      [[ "$reclaimed" == $'"'"'1\talpha'"'"' ]] || fail "dead claim was not requeued: [$reclaimed]"
      claim_pid="$(cut -f3 "$QUEUE_DIR/claims/1")"
      [[ "$claim_pid" == "$$" ]] || fail "requeued claim did not record the new worker pid"
      [[ "$(cut -f4 "$QUEUE_DIR/claims/1")" == "2" ]] || fail "requeued claim did not advance the attempt"

      reset_queue
      printf "alpha\nbeta\n" > "$TASK_FILE"
      mkdir -p "$QUEUE_DIR/claims"
      printf "1\talpha\t%s\t1\n" "$$" > "$QUEUE_DIR/claims/1"
      echo 2 > "$NEXT_INDEX_FILE"
      next_task="$(harbor_pick_task || true)"
      [[ "$next_task" == $'"'"'2\tbeta'"'"' ]] || fail "live claim was stolen: [$next_task]"
      [[ -f "$QUEUE_DIR/claims/1" ]] || fail "live claim was removed"

      reset_queue
      printf "alpha\nbeta\n" > "$TASK_FILE"
      mkdir -p "$QUEUE_DIR/claims"
      printf "1\talpha\t%s\t1\n" "$(dead_pid)" > "$QUEUE_DIR/claims/1"
      printf "1\talpha\t1.0\t\t/tmp/result.json\n" >> "$QUEUE_DIR/done.txt"
      echo 2 > "$NEXT_INDEX_FILE"
      recorded="$(harbor_pick_task || true)"
      [[ "$recorded" == $'"'"'2\tbeta'"'"' ]] || fail "recorded claim was retried: [$recorded]"
      [[ ! -f "$QUEUE_DIR/claims/1" ]] || fail "recorded dead claim was left behind"
      if [[ -s "$QUEUE_DIR/reclaim.txt" ]] && grep -q "^1	" "$QUEUE_DIR/reclaim.txt"; then
        fail "recorded claim was written back to the reclaim list"
      fi

      reset_queue
      printf "alpha\nbeta\n" > "$TASK_FILE"
      picked="$(harbor_pick_task || true)"
      harbor_release_claim "${picked%%$'"'"'\t'"'"'*}"
      [[ ! -e "$QUEUE_DIR/claims/1" ]] || fail "release left the claim in place"

      reset_queue
      printf "alpha\n" > "$TASK_FILE"
      mkdir -p "$QUEUE_DIR/claims"
      printf "1\talpha\t%s\t2\n" "$(dead_pid)" > "$QUEUE_DIR/claims/1"
      printf "1\talpha\t%s\n" "$(dead_pid)" > "$QUEUE_DIR/worker-7.current"
      echo 2 > "$NEXT_INDEX_FILE"
      harbor_recover_lost_claims
      [[ "$(harbor_count_live_workers)" == "0" ]] || fail "dead current file still counts as running"
      [[ ! -e "$QUEUE_DIR/worker-7.current" ]] || fail "dead current file was not cleared"
      grep -q $'"'"'^1\talpha\tworker-lost\tWorkerLost$'"'"' "$QUEUE_DIR/failed.txt" \
        || fail "exhausted claim was not recorded as failed: [$(cat "$QUEUE_DIR/failed.txt")]"
      exhausted="$(harbor_pick_task || true)"
      [[ -z "$exhausted" ]] || fail "exhausted claim was requeued: [$exhausted]"

      reset_queue
      printf "alpha\n" > "$TASK_FILE"
      mkdir -p "$QUEUE_DIR/claims"
      printf "1\talpha\t%s\t1\n" "$(dead_pid)" > "$QUEUE_DIR/claims/1"
      echo 2 > "$NEXT_INDEX_FILE"
      recovery_log="$TEST_ROOT/recovery.log"
      export HARBOR_QUEUE_RECOVERY_COMMAND="$TEST_ROOT/fake-recovery.sh"
      cat > "$HARBOR_QUEUE_RECOVERY_COMMAND" <<'"'"'EOF'"'"'
#!/usr/bin/env bash
printf "%s\n" "$1" > "$TEST_ROOT/recovery.log"
sleep 30
EOF
      chmod +x "$HARBOR_QUEUE_RECOVERY_COMMAND"
      harbor_recover_lost_claims
      harbor_ensure_queue_recovery_worker
      for _ in 1 2 3 4 5 10; do
        [[ -s "$recovery_log" ]] && break
        sleep 0.1
      done
      [[ "$(cat "$recovery_log" 2>/dev/null || true)" == "recovery" ]] \
        || fail "no worker was started for a requeued claim"
      harbor_ensure_queue_recovery_worker
      [[ "$(wc -l < "$recovery_log" | tr -d " ")" == "1" ]] \
        || fail "a second recovery worker was started"
      if [[ -f "$QUEUE_DIR/recovery.pid" ]]; then
        kill "$(tr -d "[:space:]" < "$QUEUE_DIR/recovery.pid")" 2>/dev/null || true
        wait "$(tr -d "[:space:]" < "$QUEUE_DIR/recovery.pid")" 2>/dev/null || true
      fi

      reset_queue
      printf "alpha\n" > "$TASK_FILE"
      mkdir -p "$QUEUE_DIR/claims"
      printf "1\talpha\t%s\t1\n" "$(dead_pid)" > "$QUEUE_DIR/claims/1"
      echo 2 > "$NEXT_INDEX_FILE"
      export HARBOR_QUEUE_RECOVERY_SPAWNS="1"
      export HARBOR_QUEUE_RECOVERY_COMMAND="$TEST_ROOT/fake-recovery-exit.sh"
      cat > "$HARBOR_QUEUE_RECOVERY_COMMAND" <<'"'"'EOF'"'"'
#!/usr/bin/env bash
printf "%s\n" "$1" >> "$TEST_ROOT/recovery-exit.log"
exit 1
EOF
      chmod +x "$HARBOR_QUEUE_RECOVERY_COMMAND"
      : > "$TEST_ROOT/recovery-exit.log"
      harbor_recover_lost_claims
      harbor_ensure_queue_recovery_worker
      for _ in 1 2 3 4 5 10; do
        [[ -s "$TEST_ROOT/recovery-exit.log" ]] && break
        sleep 0.1
      done
      wait "$(tr -d "[:space:]" < "$QUEUE_DIR/recovery.pid")" 2>/dev/null || true
      harbor_ensure_queue_recovery_worker
      grep -q $'"'"'^1\talpha\tworker-lost\tWorkerLost$'"'"' "$QUEUE_DIR/failed.txt" \
        || fail "recovery that never claimed the task left it pending: [$(cat "$QUEUE_DIR/failed.txt")]"
      [[ "$(wc -l < "$TEST_ROOT/recovery-exit.log" | tr -d " ")" == "1" ]] \
        || fail "recovery worker was restarted after the spawn budget"

      printf "ok\n"
    ' bash "$HARBOR_DIR/env.sh"
)"

[[ "$output" == "ok" ]]
printf "ok\n"
