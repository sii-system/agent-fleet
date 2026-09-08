#!/usr/bin/env bash
set -euo pipefail

# Run paths, model gateway, diagnostics, tracing, and runner/cache defaults.
# Sourced by ../env.sh; SCRIPT_DIR remains the Harbor directory.

AGENTS_DIR="${AGENTS_DIR:-$REPO_ROOT/Agents}"
TASKS_DIR="${TASKS_DIR:-$REPO_ROOT/Tasks}"
HARBOR_CLAUDE_CODE_DIR="${HARBOR_CLAUDE_CODE_DIR:-$AGENTS_DIR/Harbor-claude-code}"
HARBOR_OPENCODE_DIR="${HARBOR_OPENCODE_DIR:-$AGENTS_DIR/Harbor-opencode}"
HARBOR_PI_DIR="${HARBOR_PI_DIR:-$AGENTS_DIR/Harbor-pi}"
WORKSPACE_DIR="${WORKSPACE_DIR:-/workspace}"

RUN_ID="${RUN_ID:-$(date +%Y-%m-%d-%H%M)-harbor-tui}"
TOTAL_WORKERS="${TOTAL_WORKERS:-10}"
N_ATTEMPTS="${N_ATTEMPTS:-1}"
MAX_RETRIES="${MAX_RETRIES:-${HARBOR_MAX_RETRIES:-2}}"
# AGENT selects the runner: claude-code (default), opencode, or pi.
AGENT="${AGENT:-claude-code}"
MODEL="${MODEL:-minimax2.7}"
_HARBOR_EFFECTIVE_MODEL="${HARBOR_MODEL:-$MODEL}"
# OpenCode requires provider/model for custom providers. Keep MODEL shared with
# claude-code, and only add this prefix when AGENT=opencode.
OPENCODE_PROVIDER="${OPENCODE_PROVIDER:-custom}"
PI_PROVIDER="${PI_PROVIDER:-}"

HARBOR_ROOT="${HARBOR_ROOT:-/workspace/harbor}"
# Dataset selection:
#   DATASET_NAME: auto, seta, smith, terminalbench21, sweverify,
#     or a Harbor registry dataset id such as owner/name or owner/name@version.
#     seta, terminalbench21, and sweverify are registry aliases. smith is local.
#     For a local/offline checkout, use auto so the dataset is inferred from
#     DATASET_PATH.
#   DATASET_PATH examples:
#     /workspace/seta-env/Harbor-Dataset
#     /workspace/harbor/datasets/swesmith
#     /workspace/terminal-bench-2-1/tasks
#     /workspace/swebench-verified
# TASK_SOURCE_FILE can override the built-in task list under Tasks/.
DATASET_NAME="${DATASET_NAME:-auto}"
DATASET_PATH="${DATASET_PATH:-/workspace/seta-env/Harbor-Dataset}"
METRIC_MODE="${METRIC_MODE:-auto}"
HARBOR_TERMINALBENCH21_REGISTRY_ID="terminal-bench/terminal-bench-2-1"

# Host-direct runs must not assume that the caller can write to /workspace.
# The checkout-local runs directory is ignored by git, mounted at the same path
# by dind-run.sh, and remains overrideable for managed deployments.
OUTPUT_ROOT="${OUTPUT_ROOT:-$REPO_ROOT/runs}"
OUTPUT_PATH="${OUTPUT_PATH:-${OUTPUT_ROOT}/${RUN_ID}}"
TASK_SOURCE_FILE="${TASK_SOURCE_FILE:-}"
TASK_FILE="${TASK_FILE:-${OUTPUT_PATH}/tasks.txt}"
FLEET_TASKS="${FLEET_TASKS:-}"
# Per-agent state so toggling AGENT between runs in the same OUTPUT_PATH
# cannot cross-contaminate queue/wheel/image state. TASK_FILE stays shared.
QUEUE_DIR="${QUEUE_DIR:-${OUTPUT_PATH}/queue/${AGENT}}"
RUNTIME_DIR="${RUNTIME_DIR:-${OUTPUT_PATH}/runtime/${AGENT}}"
LAYOUT_FILE="${LAYOUT_FILE:-${OUTPUT_PATH}/harbor-layout.kdl}"
JOBS_ROOT="${JOBS_ROOT:-${OUTPUT_PATH}/jobs/${AGENT}}"
HARBOR_ONLINE_ANALYSIS="${HARBOR_ONLINE_ANALYSIS:-0}"
HARBOR_ONLINE_ANALYSIS_POLL_INTERVAL="${HARBOR_ONLINE_ANALYSIS_POLL_INTERVAL:-1}"
HARBOR_ONLINE_ANALYSIS_DIR="${HARBOR_ONLINE_ANALYSIS_DIR:-${OUTPUT_PATH}/online-analysis}"
HARBOR_ONLINE_ANALYSIS_PID_FILE="${HARBOR_ONLINE_ANALYSIS_PID_FILE:-${RUNTIME_DIR}/online-rule-analyzer.pid}"
HARBOR_ONLINE_ANALYSIS_LOG_FILE="${HARBOR_ONLINE_ANALYSIS_LOG_FILE:-${RUNTIME_DIR}/online-rule-analyzer.log}"
HARBOR_EARLY_STOP="${HARBOR_EARLY_STOP:-0}"
HARBOR_ZELLIJ_CLOSE_ON_COMPLETE="${HARBOR_ZELLIJ_CLOSE_ON_COMPLETE:-1}"
HARBOR_ZELLIJ_KEEP_ON_FAILURE="${HARBOR_ZELLIJ_KEEP_ON_FAILURE:-}"
HARBOR_MONITOR_ENABLED="${HARBOR_MONITOR_ENABLED:-1}"
HARBOR_MONITOR_DIR="${HARBOR_MONITOR_DIR:-${OUTPUT_PATH}/monitor}"
HARBOR_MONITOR_PID_FILE="${HARBOR_MONITOR_PID_FILE:-${RUNTIME_DIR}/harbor-monitor.pid}"
HARBOR_MONITOR_LOG_FILE="${HARBOR_MONITOR_LOG_FILE:-${RUNTIME_DIR}/harbor-monitor.log}"
HARBOR_BENCHMARK_PID_FILE="${HARBOR_BENCHMARK_PID_FILE:-${RUNTIME_DIR}/harbor-benchmark.pid}"
HARBOR_BENCHMARK_EXIT_FILE="${HARBOR_BENCHMARK_EXIT_FILE:-${RUNTIME_DIR}/harbor-benchmark.exit}"
HARBOR_JOB_DIR_FILE="${HARBOR_JOB_DIR_FILE:-${RUNTIME_DIR}/harbor-job-dir}"
HARBOR_MONITOR_RESTART_CMD="${HARBOR_MONITOR_RESTART_CMD:-}"
HARBOR_MONITOR_STOP_CMD="${HARBOR_MONITOR_STOP_CMD:-}"
HARBOR_MONITOR_INTERVAL="${HARBOR_MONITOR_INTERVAL:-30}"
HARBOR_MONITOR_STARTUP_GRACE="${HARBOR_MONITOR_STARTUP_GRACE:-300}"
HARBOR_MONITOR_STALL_SECONDS="${HARBOR_MONITOR_STALL_SECONDS:-1800}"
HARBOR_MONITOR_MAX_RETRIES="${HARBOR_MONITOR_MAX_RETRIES:-3}"
HARBOR_MONITOR_CONFIGURED_TIMEOUT="${HARBOR_MONITOR_CONFIGURED_TIMEOUT:-}"

API_KEY="${API_KEY:-xxx}"
BASE_URL="${BASE_URL:-}"
# Normalize to a versionless API root: callers may supply a value already ending
# in /v1, but the endpoints below append /v1 (or /v1/chat/completions), so strip
# one trailing /v1 to avoid doubling it.
if [[ -n "$BASE_URL" ]]; then
  BASE_URL="${BASE_URL%/}"
  BASE_URL="${BASE_URL%/v1}"
fi
HARBOR_ANTHROPIC_BASE_URL="${HARBOR_ANTHROPIC_BASE_URL:-${ANTHROPIC_BASE_URL:-${BASE_URL%/}}}"
HARBOR_ANTHROPIC_BASE_URL="${HARBOR_ANTHROPIC_BASE_URL%/}"
HARBOR_ANTHROPIC_BASE_URL="${HARBOR_ANTHROPIC_BASE_URL%/v1}"
HARBOR_ANTHROPIC_AUTH_TOKEN="${HARBOR_ANTHROPIC_AUTH_TOKEN:-${ANTHROPIC_AUTH_TOKEN:-$API_KEY}}"
HARBOR_ANALYZER_API_KEY="${HARBOR_ANALYZER_API_KEY:-$HARBOR_ANTHROPIC_AUTH_TOKEN}"
HARBOR_ANALYZER_BASE_URL="${HARBOR_ANALYZER_BASE_URL:-${HARBOR_ANTHROPIC_BASE_URL:+${HARBOR_ANTHROPIC_BASE_URL}/v1}}"
HARBOR_ANALYZER_MODEL="${HARBOR_ANALYZER_MODEL:-$_HARBOR_EFFECTIVE_MODEL}"
HARBOR_ANALYZER_PI_PROVIDER="${HARBOR_ANALYZER_PI_PROVIDER:-harbor-analyzer}"
HARBOR_ANALYZER_NO_PROXY="${HARBOR_ANALYZER_NO_PROXY:-0}"
HARBOR_ANALYZER_ENABLED="${HARBOR_ANALYZER_ENABLED:-$HARBOR_MONITOR_ENABLED}"
HARBOR_ANALYZER_MODE="${HARBOR_ANALYZER_MODE:-handover-follow}"
HARBOR_ANALYZER_OUTPUT_DIR="${HARBOR_ANALYZER_OUTPUT_DIR:-${OUTPUT_PATH}/analyzer}"
HARBOR_ANALYZER_PID_FILE="${HARBOR_ANALYZER_PID_FILE:-${RUNTIME_DIR}/harbor-analyzer.pid}"
HARBOR_ANALYZER_SUPERVISOR_PID_FILE="${HARBOR_ANALYZER_SUPERVISOR_PID_FILE:-${RUNTIME_DIR}/harbor-analyzer-supervisor.pid}"
HARBOR_ANALYZER_SUPERVISOR_ID_FILE="${HARBOR_ANALYZER_SUPERVISOR_ID_FILE:-${RUNTIME_DIR}/harbor-analyzer-supervisor.identity}"
HARBOR_ANALYZER_LOG_FILE="${HARBOR_ANALYZER_LOG_FILE:-${RUNTIME_DIR}/harbor-analyzer.log}"
HARBOR_ANALYZER_POLL_INTERVAL="${HARBOR_ANALYZER_POLL_INTERVAL:-5}"
HARBOR_ANALYZER_TIMEOUT="${HARBOR_ANALYZER_TIMEOUT:-900}"
HARBOR_ANALYZER_MAX_CONCURRENCY="${HARBOR_ANALYZER_MAX_CONCURRENCY:-1}"
HARBOR_FIXER_API_KEY="${HARBOR_FIXER_API_KEY:-$HARBOR_ANTHROPIC_AUTH_TOKEN}"
HARBOR_FIXER_BASE_URL="${HARBOR_FIXER_BASE_URL:-${HARBOR_ANTHROPIC_BASE_URL:+${HARBOR_ANTHROPIC_BASE_URL}/v1}}"
HARBOR_FIXER_MODEL="${HARBOR_FIXER_MODEL:-$_HARBOR_EFFECTIVE_MODEL}"
HARBOR_FIXER_PI_BIN="${HARBOR_FIXER_PI_BIN:-pi}"
HARBOR_FIXER_PI_PROVIDER="${HARBOR_FIXER_PI_PROVIDER:-harbor-fixer}"
HARBOR_FIXER_NO_PROXY="${HARBOR_FIXER_NO_PROXY:-0}"
HARBOR_FIXER_AGENT_TIMEOUT="${HARBOR_FIXER_AGENT_TIMEOUT:-900}"
HARBOR_FIXER_EXECUTION_TIMEOUT="${HARBOR_FIXER_EXECUTION_TIMEOUT:-300}"
HARBOR_FIXER_SUMMARY_LIMIT="${HARBOR_FIXER_SUMMARY_LIMIT:-4000}"
HARBOR_FIXER_MAX_CONCURRENCY="${HARBOR_FIXER_MAX_CONCURRENCY:-4}"
HARBOR_FIXER_MAX_TASK_SUMMARY_CHARS="${HARBOR_FIXER_MAX_TASK_SUMMARY_CHARS:-24000}"
HARBOR_FIXER_MAX_TASK_SUMMARIES_CHARS="${HARBOR_FIXER_MAX_TASK_SUMMARIES_CHARS:-400000}"
OPIK_URL="${OPIK_URL:-}"
# OPIK_URL is the operator switch for running with or without Opik. The shared
# gate also honors OPIK_TRACK_DISABLE as a runtime safety override.
harbor_trace_to_opik_enabled() {
  OPIK_URL="${OPIK_URL:-}" OPIK_TRACK_DISABLE="${OPIK_TRACK_DISABLE:-}" \
    python3 "$SCRIPT_DIR/opik_trace_gate.py"
}
OPIK_URL_OVERRIDE="${OPIK_URL_OVERRIDE:-$OPIK_URL}"
OPIK_BASE="${OPIK_BASE:-${OPIK_URL_OVERRIDE%/api}}"

harbor_run_name_component() {
  local value
  value="$(printf '%s' "$1" |
    LC_ALL=C tr -cs 'A-Za-z0-9_-' '-' |
    sed 's/^-*//; s/-*$//' |
    cut -c1-"${2:-32}")"
  value="${value#-}"
  value="${value%-}"
  printf '%s\n' "${value:-run}"
}

HARBOR_RUN_TIMESTAMP="${HARBOR_RUN_TIMESTAMP:-$(date +%Y%m%d-%H%M%S)}"
HARBOR_SESSION_TIMESTAMP="${HARBOR_SESSION_TIMESTAMP:-$(date +%H%M%S)}"
HARBOR_RUN_AGENT_NAME="$(harbor_run_name_component "$AGENT" 20)"
HARBOR_RUN_DATASET_NAME="$(harbor_run_name_component "$DATASET_NAME" 32)"
HARBOR_RUN_MODEL_NAME="$(harbor_run_name_component "$_HARBOR_EFFECTIVE_MODEL" 32)"
# Both defaults describe the effective run. Keep the Zellij name independent
# from a caller-supplied Opik project so an unrelated project cannot relabel or
# collide with the local session.
OPIK_PROJECT_NAME="${OPIK_PROJECT_NAME:-agent-fleet-${HARBOR_RUN_AGENT_NAME}-${HARBOR_RUN_DATASET_NAME}-${HARBOR_RUN_MODEL_NAME}-${HARBOR_RUN_TIMESTAMP}}"
HARBOR_ZELLIJ_SESSION_NAME="${HARBOR_ZELLIJ_SESSION_NAME:-$(harbor_run_name_component "h-${HARBOR_SESSION_TIMESTAMP}-$$-$(harbor_run_name_component "$AGENT" 8)-$(harbor_run_name_component "$DATASET_NAME" 8)-$(harbor_run_name_component "$_HARBOR_EFFECTIVE_MODEL" 8)" 40)}"
# Some launch wrappers pass the placeholder literally. Do not forward that
# into task containers, otherwise Opik auth/config becomes invalid.
if [[ "${OPIK_API_KEY:-}" == '${OPIK_API_KEY}' ]]; then
  unset OPIK_API_KEY
fi
OPIK_API_KEY="${OPIK_API_KEY:-local-dev-key}"
OPIK_WORKSPACE="${OPIK_WORKSPACE:-default}"
CC_OPIK_DEBUG="${CC_OPIK_DEBUG:-true}"

CLAUDE_CODE_VERSION="${CLAUDE_CODE_VERSION:-2.1.90}"
CLAUDE_CODE_TGZ_BASENAME="${CLAUDE_CODE_TGZ_BASENAME:-claude-code-${CLAUDE_CODE_VERSION}.tgz}"
PI_VERSION="${PI_VERSION:-0.81.1}"
PI_TGZ_BASENAME="${PI_TGZ_BASENAME:-pi-coding-agent-${PI_VERSION}.tgz}"
PI_NODE_RUNTIME_BASENAME="${PI_NODE_RUNTIME_BASENAME:-pi-node-runtime.tar.gz}"
PI_RUNTIME_BASENAME="${PI_RUNTIME_BASENAME:-pi-runtime-${PI_VERSION}.tar.gz}"
PI_THINKING_LEVEL="${PI_THINKING_LEVEL:-high}"
LOCAL_WHEEL_DIR="${LOCAL_WHEEL_DIR:-$AGENT_FLEET_CACHE_DIR/harbor-deps}"
LOCAL_WHEEL_PORT="${LOCAL_WHEEL_PORT:-18765}"
LOCAL_WHEEL_PORT_ATTEMPTS="${LOCAL_WHEEL_PORT_ATTEMPTS:-3}"
LOCAL_WHEEL_HOST_IP="${LOCAL_WHEEL_HOST_IP:-}"
if [[ -z "${LOCAL_WHEEL_HOST_IP:-}" ]] && command -v ip >/dev/null 2>&1; then
  LOCAL_WHEEL_HOST_IP="$(ip -4 addr show docker0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1 | head -n 1 || true)"
fi
if [[ -z "${LOCAL_WHEEL_HOST_IP:-}" ]] && command -v ip >/dev/null 2>&1; then
  LOCAL_WHEEL_HOST_IP="$(ip route 2>/dev/null | awk '/^default /{print $3; exit}' || true)"
fi
if [[ -z "${HARBOR_LOCAL_WHEEL_SERVER_URL:-}" && -n "${LOCAL_WHEEL_HOST_IP:-}" ]]; then
  HARBOR_LOCAL_WHEEL_SERVER_URL="http://${LOCAL_WHEEL_HOST_IP}:${LOCAL_WHEEL_PORT}"
fi
if [[ -z "${HARBOR_LOCAL_CLAUDE_TGZ_URL:-}" && -n "${HARBOR_LOCAL_WHEEL_SERVER_URL:-}" ]]; then
  HARBOR_LOCAL_CLAUDE_TGZ_URL="${HARBOR_LOCAL_WHEEL_SERVER_URL%/}/${CLAUDE_CODE_TGZ_BASENAME}"
fi
HARBOR_REMOTE_WHEEL_SERVER_URLS="${HARBOR_REMOTE_WHEEL_SERVER_URLS:-}"
EFFECTIVE_WHEEL_URL_FILE="${RUNTIME_DIR}/effective-wheel-url"
EFFECTIVE_CLAUDE_TGZ_URL_FILE="${RUNTIME_DIR}/effective-claude-tgz-url"
LOCAL_DEPS_LOG_FILE="${RUNTIME_DIR}/local-deps-prepare.log"
# Retain the original switch and status paths for compatibility. The runner is
# image-owned in DinD and setup-owned on a direct host; workloads only validate.
HARBOR_RUNNER_PREPARE="${HARBOR_RUNNER_PREPARE:-1}"
HARBOR_RUNNER_IMAGE_DIR="${HARBOR_RUNNER_IMAGE_DIR:-/opt/harbor-runner}"
HARBOR_RUNNER_HOST_DIR="${HARBOR_RUNNER_HOST_DIR:-$HOME/.local/share/agent-fleet/harbor-runner}"
if [[ -z "${HARBOR_RUNNER_DIR:-}" ]]; then
  if [[ -d "$HARBOR_RUNNER_IMAGE_DIR" ]]; then
    HARBOR_RUNNER_DIR="$HARBOR_RUNNER_IMAGE_DIR"
  else
    HARBOR_RUNNER_DIR="$HARBOR_RUNNER_HOST_DIR"
  fi
fi
if [[ -n "${HARBOR_RUNNER_PYTHON_VERSION:-}" &&
      -n "${HARBOR_RUNNER_PYTHON:-}" &&
      "$HARBOR_RUNNER_PYTHON_VERSION" != "$HARBOR_RUNNER_PYTHON" ]]; then
  echo "HARBOR_RUNNER_PYTHON_VERSION and legacy HARBOR_RUNNER_PYTHON disagree" >&2
  exit 1
fi
HARBOR_RUNNER_PYTHON_VERSION="${HARBOR_RUNNER_PYTHON_VERSION:-${HARBOR_RUNNER_PYTHON:-}}"
HARBOR_RUNNER_PYTHON_VERSION_FILE="$HARBOR_RUNNER_DIR/.agent-fleet-python-version"
if [[ -z "$HARBOR_RUNNER_PYTHON_VERSION" &&
      "$HARBOR_RUNNER_DIR" == "$HARBOR_RUNNER_HOST_DIR" &&
      -f "$HARBOR_RUNNER_PYTHON_VERSION_FILE" ]]; then
  IFS= read -r HARBOR_RUNNER_PYTHON_VERSION < "$HARBOR_RUNNER_PYTHON_VERSION_FILE"
fi
HARBOR_RUNNER_PYTHON_VERSION="${HARBOR_RUNNER_PYTHON_VERSION:-3.12.13}"
HARBOR_OPIK_BIN="${HARBOR_OPIK_BIN:-$HARBOR_RUNNER_DIR/bin/opik}"
HARBOR_CLI_BIN="${HARBOR_CLI_BIN:-$HARBOR_RUNNER_DIR/bin/harbor}"
HARBOR_OPIK_PYTHON="${HARBOR_OPIK_PYTHON:-$HARBOR_RUNNER_DIR/bin/python}"
HARBOR_RUNNER_REQUIREMENTS="${HARBOR_RUNNER_REQUIREMENTS:-$SCRIPT_DIR/runner-requirements.txt}"
HARBOR_RUNNER_PREPARE_STATUS_FILE="${RUNTIME_DIR}/harbor-runner-prepare.status"
HARBOR_RUNNER_PREPARE_LOG_FILE="${RUNTIME_DIR}/harbor-runner-prepare.log"
