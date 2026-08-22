#!/usr/bin/env bash
set -euo pipefail

RL_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARBOR_SCRIPT_DIR="${HARBOR_SCRIPT_DIR:-$(cd "$RL_SCRIPT_DIR/../common/Harbor" && pwd)}"
# The cache and Harbor adapter must follow the rollout agent even when this
# server is invoked directly rather than through the Harbor start wrapper.
if [[ -n "${RL_AGENT:-}" ]]; then
  export AGENT="$RL_AGENT"
fi
. "$HARBOR_SCRIPT_DIR/env.sh"

# Prevent a failed long-lived rollout UI process from filling the run disk.
ulimit -c 0 >/dev/null 2>&1 || true

DETACH_MODE=false
STOP_MODE=false
if [[ "${1:-}" == "--detach" ]]; then
  DETACH_MODE=true
  shift
elif [[ "${1:-}" == "--stop" ]]; then
  STOP_MODE=true
  shift
fi

server_is_running() {
  local pid="${1:-}"
  [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1
}

server_pid() {
  if [[ -f "$RL_SERVER_PID_FILE" ]]; then
    cat "$RL_SERVER_PID_FILE" 2>/dev/null || true
  fi
}

stop_server() {
  local pid
  pid="$(server_pid)"
  if server_is_running "$pid"; then
    kill "$pid" >/dev/null 2>&1 || true
    for _ in $(seq 1 10); do
      server_is_running "$pid" || break
      sleep 0.5
    done
    server_is_running "$pid" && kill -9 "$pid" >/dev/null 2>&1 || true
  fi
  rm -f "$RL_SERVER_PID_FILE"
}

require_trace_plugin_source() {
  local path="$1"
  if [[ ! -f "$path" ]]; then
    echo "trace plugin source missing: $path" >&2
    echo "initialize the repository submodules before starting the rollout listener" >&2
    return 1
  fi
}

validate_trace_plugin_source() {
  if [[ "$RL_AGENT" == "opencode" ]]; then
    # The custom OpenCode runner always uploads both files during install,
    # including when trace emission is disabled for the task.
    require_trace_plugin_source "$TRACE_PLUGIN_OPENCODE_PLUGIN_SOURCE" || return 1
    require_trace_plugin_source "$TRACE_PLUGIN_OPENCODE_HOOK_SOURCE" || return 1
  elif [[ "$RL_AGENT" == "claude-code" ]] \
    && [[ "$HARBOR_CC_OPIK_ENABLE_HOOK" == "1" ]]; then
    require_trace_plugin_source "$TRACE_PLUGIN_CLAUDE_HOOK_SOURCE"
  fi
}

if [[ "$STOP_MODE" == "true" ]]; then
  stop_server
  exit 0
fi

# Do not expose a healthy rollout endpoint that can only return empty traces.
validate_trace_plugin_source
if ! python3 "$RL_SCRIPT_DIR/rollout_worker_utils.py" request-headers >/dev/null; then
  echo "invalid MODEL_REQUEST_CONFIG_JSON; listener was not started" >&2
  exit 1
fi

mkdir -p "$RL_TRIALS_DIR" "$RL_ACTIVE_DIR" "$RL_QUEUE_DIR/pending" "$RL_QUEUE_DIR/results" \
  "$RL_JOB_QUEUE_ROOT" "$RL_JOB_RUNTIME_ROOT" "$(dirname "$RL_TRACE_LOG")" "$RUNTIME_DIR"
touch "$RL_TRACE_LOG" "$RL_SERVER_LOG"

existing_pid="$(server_pid)"
if [[ "$DETACH_MODE" == "true" && -n "$existing_pid" ]]; then
  if server_is_running "$existing_pid"; then
    printf 'rl-rollout-server pid=%s port=%s\n' "$existing_pid" "$RL_PORT"
    exit 0
  fi
  rm -f "$RL_SERVER_PID_FILE"
fi

# Prepare the local dependency cache and Harbor/Opik runner before opening the
# RL listener.  Once Polar/Miles has assigned tasks, workers should only consume
# the prepared cache instead of blocking per request on package setup.
if ! harbor_prepare_agent_runtime; then
  echo "failed to prepare rollout agent runtime; listener was not started" >&2
  exit 1
fi

if [[ "$DETACH_MODE" == "true" ]]; then
  # The listener is intentionally not inside zellij. It owns port 19001; job
  # zellij sessions are created lazily per Ray submission.
  nohup setsid env -u ZELLIJ_SESSION_NAME TERM=xterm-256color bash -lc \
    "cd '$RL_SCRIPT_DIR' && exec python3 rollout_remote_harbor.py" \
    >>"$RL_SERVER_LOG" 2>&1 &
  pid="$!"
  printf '%s\n' "$pid" >"$RL_SERVER_PID_FILE"

  for _ in $(seq 1 20); do
    if curl -fsS --max-time 2 "http://127.0.0.1:${RL_PORT}/health" >/dev/null 2>&1; then
      printf 'rl-rollout-server pid=%s port=%s\n' "$pid" "$RL_PORT"
      exit 0
    fi
    if ! server_is_running "$pid"; then
      echo "rl rollout server exited during startup; see $RL_SERVER_LOG" >&2
      exit 1
    fi
    sleep 0.5
  done

  echo "rl rollout server did not become healthy; see $RL_SERVER_LOG" >&2
  exit 1
fi

cd "$RL_SCRIPT_DIR"
exec python3 rollout_remote_harbor.py
