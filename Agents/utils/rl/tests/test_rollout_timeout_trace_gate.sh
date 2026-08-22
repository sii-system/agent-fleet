#!/usr/bin/env bash
set -euo pipefail

RL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HARBOR_DIR="$(cd "$RL_DIR/../common/Harbor" && pwd)"

# Exercise the real shared predicate and rollout timeout function without
# starting the persistent rollout worker loop.
source /dev/stdin <<EOF
$(sed -n '/^harbor_trace_to_opik_enabled()/,/^}/p' "$HARBOR_DIR/env.sh")
$(sed -n '/^finalize_timeout_trace()/,/^}/p' "$RL_DIR/run_rl_rollout_worker.sh")
EOF

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
probe="$tmp/find-trial-logs-dir.called"
LOGGED=""

log_msg() { LOGGED="${1:-}"; }
find_trial_logs_dir() {
  : > "$probe"
  printf '%s\n' ""
}

OPIK_URL=""
for RL_AGENT in claude-code opencode; do
  rm -f "$probe"
  LOGGED=""
  finalize_timeout_trace "/tmp/rollout-trace-gate-result.json"
  [[ ! -e "$probe" ]] || {
    echo "$RL_AGENT resolved logs with OPIK_URL empty" >&2
    exit 1
  }
  [[ "$LOGGED" == *"OPIK_URL is empty"* ]] || {
    echo "$RL_AGENT missing trace-off skip log: $LOGGED" >&2
    exit 1
  }
done

# Tracing on must continue into the existing logs-dir resolution path
# instead of taking the trace-off skip branch.
OPIK_URL="https://opik.example.invalid/api"
RL_AGENT=claude-code
rm -f "$probe"
LOGGED=""
finalize_timeout_trace "/tmp/rollout-trace-gate-result.json"
[[ -e "$probe" ]] || {
  echo "trace-on rollout did not resolve the trial logs dir" >&2
  exit 1
}
[[ "$LOGGED" != *"OPIK_URL is empty"* ]] || {
  echo "trace-on rollout incorrectly took the trace-off skip path: $LOGGED" >&2
  exit 1
}

echo "rollout timeout trace gate OK"
