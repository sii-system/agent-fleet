#!/usr/bin/env bash
set -euo pipefail

HARBOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d)"
trap 'if [[ -n "${orphan:-}" ]]; then kill -TERM -- "-$orphan" >/dev/null 2>&1 || true; fi; rm -rf "$TEST_ROOT"' EXIT
export HOME="$TEST_ROOT" OUTPUT_PATH="$TEST_ROOT/run" AGENT=claude-code DATASET_NAME=auto OPIK_URL=""
export API_KEY=test-only HARBOR_ANTHROPIC_AUTH_TOKEN=test-only BASE_URL=https://example.invalid
source "$HARBOR_DIR/env.sh"
harbor_init_run_dirs
export HARBOR_QUEUE_CLAIM_ATTEMPTS=2

assert_eq() {
  if [[ "$1" != "$2" ]]; then
    printf 'expected [%s], got [%s]\n' "$2" "$1" >&2
    exit 1
  fi
}

# The first process exits after claiming alpha. A live worker can reclaim it,
# but repeated observations alone must never consume the retry.
printf 'alpha\r\n \t\n# comment\nbeta' > "$TASK_FILE"
owner="$(bash -c 'source "$1/env.sh"; harbor_queue pick "$$" 1 >/dev/null; echo "$$"' bash "$HARBOR_DIR")"
assert_eq "$(harbor_queue observe)" "2 1 0"
assert_eq "$(harbor_queue observe)" "2 1 0"
[[ ! -e "$QUEUE_DIR/worker-1.current" ]]
assert_eq "$(harbor_queue pick "$$" 2)" $'1\talpha'
assert_eq "$(cat "$QUEUE_DIR/claims/1")" "$(printf '1\talpha\t%s\t2' "$$")"
harbor_queue release "$owner" 1 1
[[ -e "$QUEUE_DIR/claims/1" ]]
assert_eq "$(harbor_queue pick "$$" 3)" $'4\tbeta'
assert_eq "$(harbor_queue observe)" "2 2 2"
assert_eq "$(harbor_queue pick "$$" 4)" ""
printf '1\talpha\t1.0\n4\tbeta\t1.0\n' > "$QUEUE_DIR/done.txt"
harbor_queue release "$$" 2 1
harbor_queue release "$$" 3 4
assert_eq "$(harbor_queue observe)" "2 2 0"

python3 "$HARBOR_DIR/scripts/monitor.py" --run-dir "$OUTPUT_PATH" --output "$TEST_ROOT/report.json" >/dev/null
python3 - "$TEST_ROOT/report.json" <<'PY'
import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text())
assert report["benchmark_status"] == "completed"
assert report["task_summary_action"]["claimed"] == 2
assert report["task_summary"]["total_evaluated"] == 2
assert report["task_summary"]["complete_success"] == 2
PY

# A torn handoff leaves the durable claim ahead of the cursor. A terminal
# claim is retired and an exhausted claim gets exactly one WorkerLost row.
printf '1\n' > "$NEXT_INDEX_FILE"
printf '1\talpha\t1.0\n' > "$QUEUE_DIR/done.txt"
printf '1\talpha\t%s\t1\n' "$owner" > "$QUEUE_DIR/claims/1"
printf '4\tbeta\t%s\t2\n' "$owner" > "$QUEUE_DIR/claims/4"
assert_eq "$(harbor_queue observe)" "2 2 0"
assert_eq "$(harbor_queue pick "$$" 4)" ""
assert_eq "$(cat "$QUEUE_DIR/failed.txt")" $'4\tbeta\tworker-lost\tWorkerLost'

# Kill the last claimant. The existing controller must ask before the run-local
# restart command launches the real worker entrypoint in dry-run mode.
printf 'alpha\n' > "$TASK_FILE"
: > "$QUEUE_DIR/done.txt"
: > "$QUEUE_DIR/failed.txt"
printf '1\n' > "$NEXT_INDEX_FILE"
bash -c 'source "$1/env.sh"; harbor_queue pick "$$" original >/dev/null; kill -KILL "$$"' bash "$HARBOR_DIR" &
victim="$!"
wait "$victim" 2>/dev/null && exit 1
assert_eq "$(harbor_queue observe)" "1 1 0"
export HARBOR_DRY_RUN=1 HARBOR_ENVIRONMENT_TYPE=docker
touch "$WORKERS_READY_FILE"
cat > "$OUTPUT_PATH/restart.sh" <<'WORKER'
#!/usr/bin/env bash
set -euo pipefail
exec bash "$SCRIPT_DIR/run_harbor_worker.sh" restarted
WORKER
chmod +x "$OUTPUT_PATH/restart.sh"
monitor=(python3 "$HARBOR_DIR/scripts/monitor.py" --run-dir "$OUTPUT_PATH"
  --startup-grace 0 --restart-cmd restart.sh --output "$TEST_ROOT/report.json"
  --user-report-output "$OUTPUT_PATH/monitor/user-notify-latest.json")
"${monitor[@]}" >/dev/null
python3 - "$TEST_ROOT/report.json" <<'PYREPORT'
import json
import sys
from pathlib import Path

action = json.loads(Path(sys.argv[1]).read_text())["action"]
assert action["type"] == "notify" and action["allowed_decisions"] == ["restart"]
PYREPORT
[[ ! -s "$QUEUE_DIR/done.txt" && ! -e "$RUNTIME_DIR/worker-logs/worker-restarted.log" ]]
python3 "$HARBOR_DIR/scripts/controller.py" --run-dir "$OUTPUT_PATH" decide restart >/dev/null
"${monitor[@]}" >/dev/null
assert_eq "$(cut -f1-3 "$QUEUE_DIR/done.txt")" $'1\talpha\tdry-run'
if ! grep -Fq 'skip execution' "$OUTPUT_PATH/1-2-alpha.console.log"; then
  echo "approved worker did not reach the Harbor dry-run command" >&2
  exit 1
fi
[[ ! -e "$QUEUE_DIR/claims/1" && ! -e "$QUEUE_DIR/worker-restarted.current" ]]

# A second attempt must not read the first attempt's trial result.
rm -rf "$JOBS_ROOT/worker-restarted/1-2-alpha"
rm -f "$OUTPUT_PATH/1-2-alpha.console.log"
mkdir -p "$JOBS_ROOT/worker-restarted/1-1-alpha/trial" "$JOBS_ROOT/worker-restarted/1-alpha/trial"
printf '%s\n' '{"is_resolved": true, "verifier_result": {"rewards": {"reward": 1.0}}}' \
  > "$JOBS_ROOT/worker-restarted/1-1-alpha/trial/result.json"
cp "$JOBS_ROOT/worker-restarted/1-1-alpha/trial/result.json" \
  "$JOBS_ROOT/worker-restarted/1-alpha/trial/result.json"
: > "$QUEUE_DIR/done.txt"
: > "$QUEUE_DIR/failed.txt"
printf '1\talpha\t%s\t1\n' "$owner" > "$QUEUE_DIR/claims/1"
printf '2\n' > "$NEXT_INDEX_FILE"
bash "$HARBOR_DIR/run_harbor_worker.sh" restarted
[[ -d "$JOBS_ROOT/worker-restarted/1-2-alpha" ]]
[[ -f "$OUTPUT_PATH/1-2-alpha.console.log" ]]
if python3 "$HARBOR_DIR/harbor_worker_utils.py" latest-result \
  "$JOBS_ROOT/worker-restarted/1-2-alpha" >/dev/null; then
  echo "attempt 2 discovered attempt 1's trial result" >&2
  exit 1
fi
assert_eq "$(cut -f1-3 "$QUEUE_DIR/done.txt")" $'1\talpha\tdry-run'

# A dead claim shell does not release a task whose session is still running.
printf 'alpha\nbeta\n' > "$TASK_FILE"
: > "$QUEUE_DIR/done.txt"
: > "$QUEUE_DIR/failed.txt"
rm -rf "$QUEUE_DIR/claims"
mkdir -p "$QUEUE_DIR/claims"
rm -f "$QUEUE_DIR"/worker-*.current
printf '1\n' > "$NEXT_INDEX_FILE"
python3 -c 'import os, time; os.setsid(); time.sleep(60)' >/dev/null 2>&1 &
orphan="$!"
pgid=""
for _ in 1 2 3 4 5 10 11 12 13 14 15; do
  pgid="$(ps -o pgid= -p "$orphan" 2>/dev/null | tr -d ' ' || true)"
  [[ "$pgid" == "$orphan" ]] && break
  sleep 0.05
done
[[ "$pgid" == "$orphan" ]]
printf '1\talpha\t0\t1\t%s\n' "$orphan" > "$QUEUE_DIR/claims/1"
printf '2\n' > "$NEXT_INDEX_FILE"
assert_eq "$(harbor_queue observe)" "2 1 1"
assert_eq "$(harbor_queue pick "$$" other)" $'2\tbeta'
[[ "$(cut -f5 "$QUEUE_DIR/claims/1")" == "$orphan" ]]
kill -TERM -- "-$orphan" >/dev/null 2>&1 || kill "$orphan" >/dev/null 2>&1 || true
wait "$orphan" 2>/dev/null || true
harbor_queue release "$$" other 2
assert_eq "$(harbor_queue pick "$$" retry)" $'1\talpha'
assert_eq "$(cut -f4 "$QUEUE_DIR/claims/1")" "2"
harbor_queue release "$$" retry 1

# Resume a pending retry written by the earlier reclaim-list format.
: > "$QUEUE_DIR/done.txt"
printf '1\talpha\t1\n' > "$QUEUE_DIR/reclaim.txt"
assert_eq "$(harbor_queue pick "$$" 5)" $'1\talpha'
assert_eq "$(cut -f4 "$QUEUE_DIR/claims/1")" "2"
[[ ! -e "$QUEUE_DIR/reclaim.txt" ]]

printf 'ok\n'
