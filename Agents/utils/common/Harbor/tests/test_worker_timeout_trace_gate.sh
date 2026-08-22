#!/usr/bin/env bash
set -euo pipefail

# The worker's timeout finalization replays hook backups into Opik for both
# agents. With OPIK_URL empty it must return before touching anything;
# with tracing on it must keep its existing behavior. Extract the shared
# helper and the function under test instead of running the worker loop.
HARBOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

source /dev/stdin <<EOF
$(sed -n '/^harbor_trace_to_opik_enabled()/,/^}/p' "$HARBOR_DIR/env.sh")
$(sed -n '/^finalize_timeout_trace()/,/^}/p' "$HARBOR_DIR/run_harbor_worker.sh")
EOF

LOGGED=""
log_msg() { LOGGED="${1:-}"; }
find_trial_logs_dir() {
  echo "find_trial_logs_dir must not run with tracing off" >&2
  exit 1
}
harbor_agent_is_opencode() {
  echo "agent dispatch must not run with tracing off" >&2
  exit 1
}

OPIK_URL=""
finalize_timeout_trace "/tmp/trace-gate-result.json"
[[ "$LOGGED" == *"OPIK_URL is empty"* ]] || {
  echo "missing trace-off skip log, got: $LOGGED" >&2
  exit 1
}

# Tracing on must still reach the logs-dir resolution (empty here, so the
# function logs the missing-dir skip instead of finalizing).
find_trial_logs_dir() { echo ""; }
OPIK_URL="https://opik.example.invalid/api"
LOGGED=""
finalize_timeout_trace "/tmp/trace-gate-result.json"
[[ "$LOGGED" == *"missing logs dir"* ]] || {
  echo "trace-on path did not reach logs-dir resolution, got: $LOGGED" >&2
  exit 1
}

echo "worker timeout trace gate OK"
