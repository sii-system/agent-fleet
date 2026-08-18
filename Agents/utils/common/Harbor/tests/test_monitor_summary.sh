#!/usr/bin/env bash
set -euo pipefail

HARBOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_TMP_DIR="$(mktemp -d)"
SERVER_PID=""
MONITOR_PID=""
ANALYZER_PID=""

cleanup() {
  [[ -n "$MONITOR_PID" ]] && kill "$MONITOR_PID" 2>/dev/null || true
  if [[ -n "$ANALYZER_PID" ]]; then
    kill "$ANALYZER_PID" 2>/dev/null || true
    wait "$ANALYZER_PID" 2>/dev/null || true
  fi
  if [[ -n "$SERVER_PID" ]]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -rf "$TEST_TMP_DIR"
}

start_monitor() {
  local out="$1"
  local close_on_complete="$2"
  local online_analysis="$3"
  local log="$4"
  env -i \
    PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
    HOME="$TEST_TMP_DIR/home" \
    TERM=dumb \
    RUN_ID="monitor-summary-test" \
    AGENT="claude-code" \
    DATASET_NAME="auto" \
    DATASET_PATH="$TEST_TMP_DIR/dataset" \
    OUTPUT_PATH="$out" \
    TOTAL_WORKERS="2" \
    LOCAL_WHEEL_DIR="$TEST_TMP_DIR/no-local-wheels" \
    CLAUDE_CODE_TGZ_BASENAME="claude-code-test.tgz" \
    HARBOR_REMOTE_WHEEL_SERVER_URLS="http://127.0.0.1:$WHEEL_PORT" \
    HARBOR_RUNNER_PREPARE="0" \
    HARBOR_ONLINE_ANALYSIS="$online_analysis" \
    HARBOR_ZELLIJ_CLOSE_ON_COMPLETE="$close_on_complete" \
    OPIK_PROJECT_NAME="monitor-summary-test" \
    OPIK_URL_OVERRIDE="http://opik.example/api" \
    bash "$HARBOR_DIR/monitor_harbor.sh" >"$log" 2>&1 &
  MONITOR_PID="$!"
}
trap cleanup EXIT

start_fake_wheel_server() {
  local srv="$TEST_TMP_DIR/wheelsrv"
  mkdir -p "$srv"
  printf 'cache_schema=3\n' > "$srv/manifest.txt"
  : > "$srv/claude-code-test.tgz"
  : > "$srv/npm-cache-ready"

  WHEEL_PORT="$(python3 - <<'PY'
import socket
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
print(sock.getsockname()[1])
sock.close()
PY
)"
  python3 -m http.server "$WHEEL_PORT" --bind 127.0.0.1 --directory "$srv" \
    >"$TEST_TMP_DIR/wheelsrv.log" 2>&1 &
  SERVER_PID="$!"

  local attempt
  for attempt in $(seq 1 50); do
    if python3 - "$WHEEL_PORT" <<'PY' 2>/dev/null
import sys
import urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
opener.open(f"http://127.0.0.1:{sys.argv[1]}/manifest.txt", timeout=1)
PY
    then
      return 0
    fi
    sleep 0.1
  done
  echo "fake wheel server did not become ready" >&2
  return 1
}

main() {
  start_fake_wheel_server

  local out="$TEST_TMP_DIR/run"
  local queue="$out/queue/claude-code"
  local analyzer_pid_file="$out/runtime/claude-code/online-rule-analyzer.pid"
  mkdir -p "$out" "$queue" "$out/runtime/claude-code" "$TEST_TMP_DIR/dataset/task-a"

  # 3 tasks, all claimed: 2 done (one rewarded), 1 failed with an exception.
  printf 'task-a\ntask-b\ntask-c\n' > "$out/tasks.txt"
  printf '1\ttask-a\t1.0\n2\ttask-b\t0.0\n' > "$queue/done.txt"
  printf '3\ttask-c\t\tTimeoutError\n' > "$queue/failed.txt"
  printf '4\n' > "$queue/next_index"

  setsid python3 "$HARBOR_DIR/scripts/online_rule_analyzer.py" \
    "$out" --follow --profile harbor --poll-interval 0.1 \
    --output-dir "$out/online-analysis" >"$TEST_TMP_DIR/analyzer.log" 2>&1 &
  ANALYZER_PID="$!"
  printf '%s\n' "$ANALYZER_PID" > "$analyzer_pid_file"

  local log="$TEST_TMP_DIR/monitor.log"
  start_monitor "$out" "1" "1" "$log"

  local deadline=$((SECONDS + 60))
  while kill -0 "$MONITOR_PID" 2>/dev/null; do
    if [[ "$SECONDS" -ge "$deadline" ]]; then
      cat "$log" >&2
      echo "monitor did not exit within 60s after all tasks finished" >&2
      return 1
    fi
    sleep 0.5
  done

  local status=0
  wait "$MONITOR_PID" || status=$?
  MONITOR_PID=""
  if [[ "$status" -ne 0 ]]; then
    cat "$log" >&2
    echo "monitor exited with status $status" >&2
    return 1
  fi

  local analyzer_deadline=$((SECONDS + 10))
  while kill -0 "$ANALYZER_PID" 2>/dev/null; do
    if [[ "$SECONDS" -ge "$analyzer_deadline" ]]; then
      echo "online analyzer was not stopped on completion" >&2
      return 1
    fi
    sleep 0.1
  done
  wait "$ANALYZER_PID" 2>/dev/null || true
  ANALYZER_PID=""
  if [[ -e "$analyzer_pid_file" ]]; then
    echo "online analyzer pid file was not removed" >&2
    return 1
  fi

  if ! grep -q "all tasks finished; summary saved to $out/summary.txt" "$log"; then
    cat "$log" >&2
    echo "monitor did not report the summary location" >&2
    return 1
  fi

  local summary="$out/summary.txt"
  if [[ ! -f "$summary" ]]; then
    echo "missing summary file: $summary" >&2
    return 1
  fi

  local pattern
  for pattern in \
    '^finished_at: ' \
    '^total: +3$' \
    '^done: +2$' \
    '^failed: +1$' \
    '^reward=1\.0: 1$' \
    '^TimeoutError: 1$' \
    "^  done: +$queue/done.txt$" \
    "^  failed: +$queue/failed.txt$"
  do
    if ! grep -Eq "$pattern" "$summary"; then
      cat "$summary" >&2
      echo "summary missing expected pattern: $pattern" >&2
      return 1
    fi
  done

  rm -f "$summary"
  local keep_log="$TEST_TMP_DIR/monitor-keep.log"
  start_monitor "$out" "0" "0" "$keep_log"
  local keep_deadline=$((SECONDS + 10))
  while [[ ! -f "$summary" ]]; do
    if ! kill -0 "$MONITOR_PID" 2>/dev/null; then
      cat "$keep_log" >&2
      echo "monitor exited despite HARBOR_ZELLIJ_CLOSE_ON_COMPLETE=0" >&2
      return 1
    fi
    if [[ "$SECONDS" -ge "$keep_deadline" ]]; then
      cat "$keep_log" >&2
      echo "monitor did not write summary in keep-open mode" >&2
      return 1
    fi
    sleep 0.1
  done
  if ! grep -q 'keeping final monitor pane open' "$keep_log"; then
    cat "$keep_log" >&2
    echo "monitor did not report keep-open mode" >&2
    return 1
  fi
  kill "$MONITOR_PID" 2>/dev/null || true
  wait "$MONITOR_PID" 2>/dev/null || true
  MONITOR_PID=""

  echo "ok"
}

main "$@"
