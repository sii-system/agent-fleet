#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"

# Load shared site configuration with the same precedence used by the top-level
# CLI and DinD entry point: runtime/exported values, config.local.env,
# config.env, then the defaults below.
# shellcheck source=../../../../scripts/config_loader.sh
source "$REPO_ROOT/scripts/config_loader.sh"
agent_fleet_load_config "$REPO_ROOT"

# Keep setup-managed and explicitly supplied prerequisite directories visible
# for direct Harbor entry points as well as scripts/run_fleet.sh.
# shellcheck source=../../../../scripts/prerequisites.sh
source "$REPO_ROOT/scripts/prerequisites.sh"
agent_fleet_prerequisite_init_path
agent_fleet_prerequisite_init_runtime

AGENTS_DIR="${AGENTS_DIR:-$REPO_ROOT/Agents}"
TASKS_DIR="${TASKS_DIR:-$REPO_ROOT/Tasks}"
HARBOR_CLAUDE_CODE_DIR="${HARBOR_CLAUDE_CODE_DIR:-$AGENTS_DIR/Harbor-claude-code}"
HARBOR_OPENCODE_DIR="${HARBOR_OPENCODE_DIR:-$AGENTS_DIR/Harbor-opencode}"
WORKSPACE_DIR="${WORKSPACE_DIR:-/workspace}"

RUN_ID="${RUN_ID:-$(date +%Y-%m-%d-%H%M)-harbor-tui}"
TOTAL_WORKERS="${TOTAL_WORKERS:-10}"
N_ATTEMPTS="${N_ATTEMPTS:-1}"
MAX_RETRIES="${MAX_RETRIES:-${TB_MAX_RETRIES:-2}}"
# AGENT selects the runner: claude-code (default) or opencode.
AGENT="${AGENT:-claude-code}"
MODEL="${MODEL:-minimax2.7}"
_HARBOR_EFFECTIVE_MODEL="${TB_MODEL:-$MODEL}"
# OpenCode requires provider/model for custom providers. Keep MODEL shared with
# claude-code, and only add this prefix when AGENT=opencode.
OPENCODE_PROVIDER="${OPENCODE_PROVIDER:-custom}"

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
DATASET_PATH="${DATASET_PATH:-${TB_PATH:-/workspace/seta-env/Harbor-Dataset}}"
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
TB_ANTHROPIC_BASE_URL="${TB_ANTHROPIC_BASE_URL:-${ANTHROPIC_BASE_URL:-${BASE_URL%/}}}"
TB_ANTHROPIC_BASE_URL="${TB_ANTHROPIC_BASE_URL%/}"
TB_ANTHROPIC_BASE_URL="${TB_ANTHROPIC_BASE_URL%/v1}"
TB_ANTHROPIC_AUTH_TOKEN="${TB_ANTHROPIC_AUTH_TOKEN:-${ANTHROPIC_AUTH_TOKEN:-$API_KEY}}"
HARBOR_ANALYZER_API_KEY="${HARBOR_ANALYZER_API_KEY:-$TB_ANTHROPIC_AUTH_TOKEN}"
HARBOR_ANALYZER_BASE_URL="${HARBOR_ANALYZER_BASE_URL:-${TB_ANTHROPIC_BASE_URL:+${TB_ANTHROPIC_BASE_URL}/v1}}"
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
TRACE_TO_OPIK="${TRACE_TO_OPIK:-true}"
# The single switch for running without Opik, shared by every script that
# sources env.sh (harboropik.sh, run_harbor_worker.sh). Anything except an
# explicit false/0 keeps tracing on, so default behavior is unchanged.
harbor_trace_to_opik_enabled() {
  case "${TRACE_TO_OPIK:-true}" in
    false|0) return 1 ;;
    *) return 0 ;;
  esac
}
OPIK_URL="${OPIK_URL:-}"
OPIK_URL_OVERRIDE="${OPIK_URL_OVERRIDE:-$OPIK_URL}"
OPIK_BASE="${OPIK_BASE:-${OPIK_URL_OVERRIDE%/api}}"
OPIK_MODE="${OPIK_MODE:-remote}"

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

CLAUDE_CODE_VERSION="${CLAUDE_CODE_VERSION:-${TB_AK_VERSION:-2.1.90}}"
CLAUDE_CODE_TGZ_BASENAME="${CLAUDE_CODE_TGZ_BASENAME:-claude-code-${CLAUDE_CODE_VERSION}.tgz}"
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
if [[ -z "${TB_LOCAL_WHEEL_SERVER_URL:-}" && -n "${LOCAL_WHEEL_HOST_IP:-}" ]]; then
  TB_LOCAL_WHEEL_SERVER_URL="http://${LOCAL_WHEEL_HOST_IP}:${LOCAL_WHEEL_PORT}"
fi
if [[ -z "${TB_LOCAL_CLAUDE_TGZ_URL:-}" && -n "${TB_LOCAL_WHEEL_SERVER_URL:-}" ]]; then
  TB_LOCAL_CLAUDE_TGZ_URL="${TB_LOCAL_WHEEL_SERVER_URL%/}/${CLAUDE_CODE_TGZ_BASENAME}"
fi
TB_REMOTE_WHEEL_SERVER_URLS="${TB_REMOTE_WHEEL_SERVER_URLS:-}"
EFFECTIVE_WHEEL_URL_FILE="${RUNTIME_DIR}/effective-wheel-url"
EFFECTIVE_CLAUDE_TGZ_URL_FILE="${RUNTIME_DIR}/effective-claude-tgz-url"
LOCAL_DEPS_LOG_FILE="${RUNTIME_DIR}/local-deps-prepare.log"
# Retain the original switch and status paths for compatibility. The runner is
# image-owned in DinD and setup-owned on a direct host; workloads only validate.
HARBOR_RUNNER_PREPARE="${HARBOR_RUNNER_PREPARE:-1}"
HARBOR_RUNNER_IMAGE_DIR="${HARBOR_RUNNER_IMAGE_DIR:-/opt/harbor-runner}"
HARBOR_RUNNER_HOST_DIR="${HARBOR_RUNNER_HOST_DIR:-$HOME/.local/share/agent-fleet/harbor-runner}"
HARBOR_RUNNER_PYTHON_VERSION="${HARBOR_RUNNER_PYTHON_VERSION:-3.12.13}"
if [[ -z "${HARBOR_RUNNER_DIR:-}" ]]; then
  if [[ -d "$HARBOR_RUNNER_IMAGE_DIR" ]]; then
    HARBOR_RUNNER_DIR="$HARBOR_RUNNER_IMAGE_DIR"
  else
    HARBOR_RUNNER_DIR="$HARBOR_RUNNER_HOST_DIR"
  fi
fi
HARBOR_OPIK_BIN="${HARBOR_OPIK_BIN:-$HARBOR_RUNNER_DIR/bin/opik}"
HARBOR_CLI_BIN="${HARBOR_CLI_BIN:-$HARBOR_RUNNER_DIR/bin/harbor}"
HARBOR_OPIK_PYTHON="${HARBOR_OPIK_PYTHON:-$HARBOR_RUNNER_DIR/bin/python}"
HARBOR_RUNNER_REQUIREMENTS="${HARBOR_RUNNER_REQUIREMENTS:-$SCRIPT_DIR/runner-requirements.txt}"
HARBOR_RUNNER_PREPARE_STATUS_FILE="${RUNTIME_DIR}/harbor-runner-prepare.status"
HARBOR_RUNNER_PREPARE_LOG_FILE="${RUNTIME_DIR}/harbor-runner-prepare.log"

# Harbor CLI compatibility aliases. Keep all defaults here so harboropik.sh and
# the zellij worker scripts cannot drift into different model/network settings.
TB_DATASET_GIT_URL="${TB_DATASET_GIT_URL:-https://huggingface.co/datasets/zai-org/terminal-bench-2-verified}"
TB_PATH="${TB_PATH:-$DATASET_PATH}"
TB_LIMIT="${TB_LIMIT:-}"
TB_RUNS="${TB_RUNS:-$N_ATTEMPTS}"
TB_AGENT="${TB_AGENT:-$AGENT}"
TB_AGENT_IMPORT_PATH="${TB_AGENT_IMPORT_PATH:-}"
TB_MODEL="${TB_MODEL:-$_HARBOR_EFFECTIVE_MODEL}"
if [[ "$AGENT" == "opencode" && "$TB_MODEL" != */* && -n "$OPENCODE_PROVIDER" ]]; then
  TB_MODEL="${OPENCODE_PROVIDER}/${TB_MODEL}"
fi
INCLUDE_TASKS="${INCLUDE_TASKS:-${TB_INCLUDE_TASKS:-}}"
TB_DRY_RUN="${TB_DRY_RUN:-0}"
MIN_TEST="${MIN_TEST:-0}"
MIN_TEST_INCLUDE_TASK="${MIN_TEST_INCLUDE_TASK:-fix-git}"
TB_N_CONCURRENT="${TB_N_CONCURRENT:-$TOTAL_WORKERS}"
TB_MAX_RETRIES="${TB_MAX_RETRIES:-$MAX_RETRIES}"
TB_RETRY_INCLUDE_EXCEPTIONS="${TB_RETRY_INCLUDE_EXCEPTIONS-}"
TB_RETRY_EXCLUDE_EXCEPTIONS="${TB_RETRY_EXCLUDE_EXCEPTIONS-RewardFileNotFoundError,RewardFileEmptyError,VerifierOutputParseError}"
TB_AK_MAX_TURNS="${TB_AK_MAX_TURNS:-}"
TB_AK_COLLECT_ROLLOUT_DETAILS="${TB_AK_COLLECT_ROLLOUT_DETAILS:-}"
TB_AK_ENABLE_SUMMARIZE="${TB_AK_ENABLE_SUMMARIZE:-}"
TB_DISALLOWED_TOOLS="${TB_DISALLOWED_TOOLS:-WebSearch WebFetch RemoteTrigger AskUserQuestion}"
TB_APPEND_SYSTEM_PROMPT="${TB_APPEND_SYSTEM_PROMPT:-Use English only for all reasoning, messages, filenames, and tool arguments. Use ASCII characters only unless reading existing non-ASCII file contents is strictly necessary.}"
TB_API_BASE="${TB_API_BASE:-${TB_ANTHROPIC_BASE_URL%/}/v1/chat/completions}"
ROLLOUT="${ROLLOUT:-0}"
HARBOR_TEMPERATURE="${HARBOR_TEMPERATURE:-}"
HARBOR_TOP_P="${HARBOR_TOP_P:-}"
HARBOR_MAX_TOKENS="${HARBOR_MAX_TOKENS:-}"
if [[ "$ROLLOUT" == "1" ]]; then
  HARBOR_TEMPERATURE=""
  HARBOR_TOP_P=""
  HARBOR_MAX_TOKENS=""
fi
if [[ -z "${TB_LLM_KWARGS:-}" ]]; then
  TB_LLM_KWARGS="$(
    TB_ANTHROPIC_AUTH_TOKEN="$TB_ANTHROPIC_AUTH_TOKEN" \
    HARBOR_TEMPERATURE="$HARBOR_TEMPERATURE" \
    HARBOR_TOP_P="$HARBOR_TOP_P" \
      python3 "$SCRIPT_DIR/env.py" llm-kwargs
  )"
fi
_HARBOR_OUTPUT_TOKEN_LIMIT="${HARBOR_MAX_TOKENS:-65536}"
TB_MAX_NEW_TOKENS="${TB_MAX_NEW_TOKENS:-$_HARBOR_OUTPUT_TOKEN_LIMIT}"
TB_MODEL_INFO="${TB_MODEL_INFO:-}"
if [[ -z "$TB_MODEL_INFO" ]]; then
  TB_MODEL_INFO="$(
    _HARBOR_OUTPUT_TOKEN_LIMIT="$_HARBOR_OUTPUT_TOKEN_LIMIT" \
      python3 "$SCRIPT_DIR/env.py" model-info
  )"
fi
TB_ANTHROPIC_CUSTOM_HEADERS="${TB_ANTHROPIC_CUSTOM_HEADERS:-${ANTHROPIC_CUSTOM_HEADERS:-}}"
TB_CLAUDE_CODE_MAX_OUTPUT_TOKENS="${TB_CLAUDE_CODE_MAX_OUTPUT_TOKENS:-$_HARBOR_OUTPUT_TOKEN_LIMIT}"
TB_CLAUDE_CODE_DISABLE_AUTOUPDATER="${TB_CLAUDE_CODE_DISABLE_AUTOUPDATER:-1}"

# Advanced Claude Code model routing defaults follow Harbor's effective task
# model. This keeps direct TB_MODEL compatibility scoped to Harbor without
# promoting it into the repository-wide MODEL variable.
TB_ANTHROPIC_MODEL="${TB_ANTHROPIC_MODEL:-$TB_MODEL}"
TB_ANTHROPIC_DEFAULT_OPUS_MODEL="${TB_ANTHROPIC_DEFAULT_OPUS_MODEL:-$TB_MODEL}"
TB_ANTHROPIC_DEFAULT_SONNET_MODEL="${TB_ANTHROPIC_DEFAULT_SONNET_MODEL:-$TB_MODEL}"
TB_ANTHROPIC_DEFAULT_HAIKU_MODEL="${TB_ANTHROPIC_DEFAULT_HAIKU_MODEL:-$TB_MODEL}"
TB_CLAUDE_CODE_SUBAGENT_MODEL="${TB_CLAUDE_CODE_SUBAGENT_MODEL:-$TB_MODEL}"
TB_CLAUDE_CODE_EFFORT_LEVEL="${TB_CLAUDE_CODE_EFFORT_LEVEL:-max}"

TB_TIMEOUT_MULTIPLIER="${TB_TIMEOUT_MULTIPLIER:-3.0}"
# Overrides only the agent execution timeout. Leave empty to use TB_TIMEOUT_MULTIPLIER.
TB_AGENT_TIMEOUT_MULTIPLIER="${TB_AGENT_TIMEOUT_MULTIPLIER:-}"
TB_AGENT_SETUP_TIMEOUT_MULTIPLIER="${TB_AGENT_SETUP_TIMEOUT_MULTIPLIER:-20}"
# Set to 1 only when Harbor prebuilt task images fail to pull from registry mirrors;
# this bypasses prebuilt pulls and builds from each task's local Dockerfile instead.
TB_FORCE_BUILD="${TB_FORCE_BUILD:-0}"
TB_DEBUG="${TB_DEBUG:-0}"
# The realtime hook default follows the tracing switch: without an Opik
# server there is nothing for the hook to talk to. An explicit value wins.
case "${TRACE_TO_OPIK:-true}" in
  false|0) TB_CC_OPIK_ENABLE_HOOK="${TB_CC_OPIK_ENABLE_HOOK:-0}" ;;
  *) TB_CC_OPIK_ENABLE_HOOK="${TB_CC_OPIK_ENABLE_HOOK:-1}" ;;
esac
TRACE_PLUGIN_SOURCE_DIR="${TRACE_PLUGIN_SOURCE_DIR:-$REPO_ROOT/third_party/agent-opik-plugin}"
TRACE_PLUGIN_CLAUDE_HOOK_SOURCE="${TRACE_PLUGIN_CLAUDE_HOOK_SOURCE:-$TRACE_PLUGIN_SOURCE_DIR/src/sii_opik_plugin/claude_code/claude_realtime_trace.py}"
TRACE_PLUGIN_OPENCODE_PLUGIN_SOURCE="${TRACE_PLUGIN_OPENCODE_PLUGIN_SOURCE:-$TRACE_PLUGIN_SOURCE_DIR/harness/opencode/opik-trace.ts}"
TRACE_PLUGIN_OPENCODE_HOOK_SOURCE="${TRACE_PLUGIN_OPENCODE_HOOK_SOURCE:-$TRACE_PLUGIN_SOURCE_DIR/src/sii_opik_plugin/opencode/opencode_realtime_trace.py}"
TB_CC_HOOK_SOURCE="${TB_CC_HOOK_SOURCE:-$TRACE_PLUGIN_CLAUDE_HOOK_SOURCE}"
TB_CC_HOOK_MOUNT_PATH="${TB_CC_HOOK_MOUNT_PATH:-/opt/tb-opik/claude_realtime_trace.py}"
TB_CC_CLAUDE_TGZ_SOURCE="${TB_CC_CLAUDE_TGZ_SOURCE:-${LOCAL_WHEEL_DIR}/${CLAUDE_CODE_TGZ_BASENAME}}"
TB_CC_CLAUDE_TGZ_MOUNT_PATH="${TB_CC_CLAUDE_TGZ_MOUNT_PATH:-/opt/tb-opik/claude-code.tgz}"
TB_CC_PY_WHEEL_DIR_SOURCE="${TB_CC_PY_WHEEL_DIR_SOURCE:-$LOCAL_WHEEL_DIR}"
TB_CC_PY_WHEEL_DIR_MOUNT_PATH="${TB_CC_PY_WHEEL_DIR_MOUNT_PATH:-/opt/tb-opik/python-wheels}"
TB_CC_NPM_CACHE_MOUNT_PATH="${TB_CC_NPM_CACHE_MOUNT_PATH:-${TB_CC_PY_WHEEL_DIR_MOUNT_PATH}/npm-cache}"
TB_VERIFIER_UV_HOME="${TB_VERIFIER_UV_HOME:-}"
TB_VERIFIER_UV_BIN_DIR_MOUNT_PATH="${TB_VERIFIER_UV_BIN_DIR_MOUNT_PATH:-/opt/tb-uv-backup/bin}"
TB_E2B_VERIFIER_UV_SOURCE="${TB_E2B_VERIFIER_UV_SOURCE:-}"
TB_CC_OPIK_DEBUG="${TB_CC_OPIK_DEBUG:-$CC_OPIK_DEBUG}"
TB_CC_OPIK_INSTALL_DEPS="${TB_CC_OPIK_INSTALL_DEPS:-true}"
# Package mirror canonical names. Defaults here match config.env;
# override in config.local.env or the shell environment.
NPM_CONFIG_REGISTRY="${NPM_CONFIG_REGISTRY:-https://registry.npmjs.org}"
GO111MODULE="${GO111MODULE:-on}"
GOPROXY="${GOPROXY:-https://goproxy.cn,direct}"
GOSUMDB="${GOSUMDB:-sum.golang.google.cn}"
RUSTUP_UPDATE_ROOT="${RUSTUP_UPDATE_ROOT:-https://mirrors.tuna.tsinghua.edu.cn/rustup/rustup}"
RUSTUP_DIST_SERVER="${RUSTUP_DIST_SERVER:-https://mirrors.tuna.tsinghua.edu.cn/rustup}"
CARGO_REGISTRY_REPLACE_WITH="${CARGO_REGISTRY_REPLACE_WITH:-mirror}"
CARGO_REGISTRY_URL="${CARGO_REGISTRY_URL:-sparse+https://mirrors.tuna.tsinghua.edu.cn/crates.io-index/}"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.org/simple/}"
PIP_EXTRA_INDEX_URL="${PIP_EXTRA_INDEX_URL:-}"
PIP_TRUSTED_HOST="${PIP_TRUSTED_HOST:-}"

# OpenSandbox must always resolve to an explicit YiCloud environment. Keep the
# values empty in the public template; harboropik.sh fails before any API call
# when neither an immutable ID nor an exact environment name is configured.
YICLOUD_SANDBOX_ENVIRONMENT_ID="${YICLOUD_SANDBOX_ENVIRONMENT_ID:-}"
YICLOUD_SANDBOX_ENVIRONMENT_NAME="${YICLOUD_SANDBOX_ENVIRONMENT_NAME:-}"
YICLOUD_SANDBOX_READY_TIMEOUT_SEC="${YICLOUD_SANDBOX_READY_TIMEOUT_SEC:-300}"
YICLOUD_SANDBOX_STATUS_LOG_INTERVAL_SEC="${YICLOUD_SANDBOX_STATUS_LOG_INTERVAL_SEC:-30}"
YICLOUD_SANDBOX_RETAIN_ON_START_FAILURE="${YICLOUD_SANDBOX_RETAIN_ON_START_FAILURE:-0}"
YICLOUD_SANDBOX_RETAIN_AFTER_TRIAL="${YICLOUD_SANDBOX_RETAIN_AFTER_TRIAL:-0}"
YICLOUD_SANDBOX_FAST_UPLOAD_ORIGIN="${YICLOUD_SANDBOX_FAST_UPLOAD_ORIGIN-https://sandbox.yicloud.com.cn}"
YICLOUD_SANDBOX_UPLOAD_TIMEOUT_SEC="${YICLOUD_SANDBOX_UPLOAD_TIMEOUT_SEC:-1800}"
YICLOUD_SANDBOX_UPLOAD_BACKEND="${YICLOUD_SANDBOX_UPLOAD_BACKEND:-http}"
YICLOUD_SANDBOX_S3_CONFIG="${YICLOUD_SANDBOX_S3_CONFIG:-}"
YICLOUD_SANDBOX_S3_BUCKET="${YICLOUD_SANDBOX_S3_BUCKET:-}"
YICLOUD_SANDBOX_S3_PREFIX="${YICLOUD_SANDBOX_S3_PREFIX:-agent-fleet-upload/v1}"
YICLOUD_SANDBOX_S3_CACHE_ROOT="${YICLOUD_SANDBOX_S3_CACHE_ROOT:-${RUNTIME_DIR}/s3-upload-cache}"
YICLOUD_SANDBOX_S3_LOCK_ROOT="${YICLOUD_SANDBOX_S3_LOCK_ROOT:-${RUNTIME_DIR}/s3-upload-locks}"
YICLOUD_SANDBOX_S3_SIGNED_URL_TTL_SEC="${YICLOUD_SANDBOX_S3_SIGNED_URL_TTL_SEC:-3600}"
YICLOUD_SANDBOX_S3_DOWNLOAD_TIMEOUT_SEC="${YICLOUD_SANDBOX_S3_DOWNLOAD_TIMEOUT_SEC:-1800}"
YICLOUD_SANDBOX_S3_DIRECTORY_COMPRESSION="${YICLOUD_SANDBOX_S3_DIRECTORY_COMPRESSION:-auto}"
YICLOUD_SANDBOX_S3CMD="${YICLOUD_SANDBOX_S3CMD:-${HARBOR_RUNNER_DIR}/bin/s3cmd}"
YICLOUD_SANDBOX_CPU="${YICLOUD_SANDBOX_CPU:-2}"
YICLOUD_SANDBOX_MEMORY="${YICLOUD_SANDBOX_MEMORY:-8Gi}"
YICLOUD_SANDBOX_LIFECYCLE_MINUTES="${YICLOUD_SANDBOX_LIFECYCLE_MINUTES:-120}"
export YICLOUD_SANDBOX_ENVIRONMENT_ID YICLOUD_SANDBOX_ENVIRONMENT_NAME
export YICLOUD_SANDBOX_READY_TIMEOUT_SEC
export YICLOUD_SANDBOX_STATUS_LOG_INTERVAL_SEC
export YICLOUD_SANDBOX_RETAIN_ON_START_FAILURE
export YICLOUD_SANDBOX_RETAIN_AFTER_TRIAL
export YICLOUD_SANDBOX_FAST_UPLOAD_ORIGIN
export YICLOUD_SANDBOX_UPLOAD_TIMEOUT_SEC YICLOUD_SANDBOX_UPLOAD_BACKEND
export YICLOUD_SANDBOX_S3_CONFIG YICLOUD_SANDBOX_S3_BUCKET
export YICLOUD_SANDBOX_S3_PREFIX YICLOUD_SANDBOX_S3_CACHE_ROOT
export YICLOUD_SANDBOX_S3_LOCK_ROOT
export YICLOUD_SANDBOX_S3_SIGNED_URL_TTL_SEC
export YICLOUD_SANDBOX_S3_DOWNLOAD_TIMEOUT_SEC
export YICLOUD_SANDBOX_S3_DIRECTORY_COMPRESSION
export YICLOUD_SANDBOX_S3CMD
export YICLOUD_SANDBOX_CPU YICLOUD_SANDBOX_MEMORY
export YICLOUD_SANDBOX_LIFECYCLE_MINUTES
UV_INDEX_URL="${UV_INDEX_URL:-$PIP_INDEX_URL}"
UV_DEFAULT_INDEX="${UV_DEFAULT_INDEX:-$UV_INDEX_URL}"

TB_PIP_DEFAULT_TIMEOUT="${TB_PIP_DEFAULT_TIMEOUT:-120}"
TB_PIP_RETRIES="${TB_PIP_RETRIES:-10}"
OPIK_REPO_DIR="${OPIK_REPO_DIR:-$HOME/sii-opik}"
COMPOSE_DIR="${COMPOSE_DIR:-$OPIK_REPO_DIR/deployment/docker-compose}"
TB_SKIP_DOCKERHUB_PREFLIGHT="${TB_SKIP_DOCKERHUB_PREFLIGHT:-0}"
TB_DOCKERHUB_CHECK_TIMEOUT="${TB_DOCKERHUB_CHECK_TIMEOUT:-8}"
TB_DOCKERHUB_PREFLIGHT_STRICT="${TB_DOCKERHUB_PREFLIGHT_STRICT:-0}"
SMITH_GENERATE_IF_MISSING="${SMITH_GENERATE_IF_MISSING:-1}"
SMITH_ADAPTER_DIR="${SMITH_ADAPTER_DIR:-$HARBOR_ROOT/adapters/swesmith}"
FIX_GIT_IMAGE_NAME="${FIX_GIT_IMAGE_NAME:-xiangyangli/fix-git:20260204}"
FIX_GIT_WARM_LABEL="${FIX_GIT_WARM_LABEL:-io.codex.prewarmed}"

# ── opencode agent ────────────────────────────────────────────────────────────
OPENCODE_VERSION="${OPENCODE_VERSION:-latest}"
OPENCODE_TGZ_BASENAME="${OPENCODE_TGZ_BASENAME:-opencode-ai-${OPENCODE_VERSION}.tgz}"
OPENCODE_LINUX_X64_TGZ_BASENAME="${OPENCODE_LINUX_X64_TGZ_BASENAME:-opencode-linux-x64-${OPENCODE_VERSION}.tgz}"
OPENCODE_CONFIG_CONTENT="${OPENCODE_CONFIG_CONTENT:-}"
if [[ "$AGENT" == "opencode" \
  && ( ( -z "$OPENCODE_CONFIG_CONTENT" && "${TB_MODEL%%/*}" == "custom" ) \
    || -n "$HARBOR_TEMPERATURE" \
    || -n "$HARBOR_TOP_P" \
    || -n "$HARBOR_MAX_TOKENS" ) ]]; then
  # OpenCode's built-in minimax provider ignores our gateway BASE_URL and calls
  # api.minimax.io directly. Use an OpenAI-compatible custom provider by default.
  OPENCODE_CONFIG_CONTENT="$(
    TB_ANTHROPIC_BASE_URL="$TB_ANTHROPIC_BASE_URL" \
    TB_ANTHROPIC_AUTH_TOKEN="$TB_ANTHROPIC_AUTH_TOKEN" \
    TB_MODEL="$TB_MODEL" \
    HARBOR_TEMPERATURE="$HARBOR_TEMPERATURE" \
    HARBOR_TOP_P="$HARBOR_TOP_P" \
    HARBOR_MAX_TOKENS="$HARBOR_MAX_TOKENS" \
    OPENCODE_CONFIG_CONTENT="$OPENCODE_CONFIG_CONTENT" \
      python3 "$SCRIPT_DIR/env.py" opencode-config
  )"
fi

NEXT_INDEX_FILE="${QUEUE_DIR}/next_index"
LOCK_FILE="${QUEUE_DIR}/.queue.lock"
WORKERS_READY_FILE="${RUNTIME_DIR}/workers.ready"
WORKERS_FAILED_FILE="${RUNTIME_DIR}/workers.failed"

# ── RL rollout mode ──────────────────────────────────────────────────────────
# Keep RL-specific implementation outside common/Harbor; Harbor only exposes the
# start.sh entry point and shared benchmark/runtime configuration.
RL_UTILS_DIR="${RL_UTILS_DIR:-$REPO_ROOT/Agents/utils/rl}"
RL_ENV_FILE="${RL_ENV_FILE:-$RL_UTILS_DIR/RL-env.sh}"
if [[ "$ROLLOUT" == "1" && -f "$RL_ENV_FILE" ]]; then
  RL_MODEL_NAME="${RL_MODEL_NAME:-$_HARBOR_EFFECTIVE_MODEL}"
  RL_API_BASE="${RL_API_BASE:-${TB_ANTHROPIC_BASE_URL:+${TB_ANTHROPIC_BASE_URL}/v1}}"
  RL_API_KEY="${RL_API_KEY:-$TB_ANTHROPIC_AUTH_TOKEN}"
  # shellcheck source=/dev/null
  . "$RL_ENV_FILE"
fi
RL_HOST="${RL_HOST:-0.0.0.0}"
RL_PORT="${RL_PORT:-19001}"
RL_DATASET_NAME="${RL_DATASET_NAME:-$DATASET_NAME}"
if [[ "$RL_DATASET_NAME" == "auto" ]]; then
  RL_DATASET_NAME="seta"
fi
RL_DATASET_ROOT="${RL_DATASET_ROOT:-$DATASET_PATH}"
RL_DATASET_ROOTS="${RL_DATASET_ROOTS:-}"
RL_TRIALS_DIR="${RL_TRIALS_DIR:-${OUTPUT_ROOT}/rl-remote-trials}"
RL_MAX_CONCURRENT="${RL_MAX_CONCURRENT:-16}"
RL_AGENT="${RL_AGENT:-claude-code}"
RL_MODEL_NAME="${RL_MODEL_NAME:-$_HARBOR_EFFECTIVE_MODEL}"
RL_MODEL_PREFIX="${RL_MODEL_PREFIX:-hosted_vllm}"
if [[ -z "${RL_API_BASE:-}" && -n "${BASE_URL:-}" ]]; then
  RL_API_BASE="${BASE_URL%/}/v1"
fi
RL_API_BASE="${RL_API_BASE:-}"
RL_API_KEY="${RL_API_KEY:-$API_KEY}"
RL_API_KEY_MODE="${RL_API_KEY_MODE:-static}"
RL_DISABLED_TASK_IDS="${RL_DISABLED_TASK_IDS:-}"
RL_ENVIRONMENT_TYPE="${RL_ENVIRONMENT_TYPE:-docker}"
RL_E2B_SANDBOX_TIMEOUT_SEC="${RL_E2B_SANDBOX_TIMEOUT_SEC:-}"
RL_E2B_PREBUILT_TEMPLATE="${RL_E2B_PREBUILT_TEMPLATE:-}"
TB_ENVIRONMENT_TYPE="${TB_ENVIRONMENT_TYPE:-$RL_ENVIRONMENT_TYPE}"
TB_E2B_SANDBOX_TIMEOUT_SEC="${TB_E2B_SANDBOX_TIMEOUT_SEC:-$RL_E2B_SANDBOX_TIMEOUT_SEC}"
TB_E2B_PREBUILT_TEMPLATE="${TB_E2B_PREBUILT_TEMPLATE:-${RL_E2B_PREBUILT_TEMPLATE:-${E2B_TEMPLATE:-}}}"
HARBOR_E2B_PREBUILT_ENVIRONMENT_SPEC="${HARBOR_E2B_PREBUILT_ENVIRONMENT_SPEC:-e2b_prebuilt:PrebuiltE2BEnvironment}"
RL_FORCE_BUILD="${RL_FORCE_BUILD:-$TB_FORCE_BUILD}"
RL_TRACE_LOG="${RL_TRACE_LOG:-${RUNTIME_DIR}/rl-rollout-requests.jsonl}"
RL_SERVER_LOG="${RL_SERVER_LOG:-${RUNTIME_DIR}/rl-rollout-server.log}"
RL_SERVER_PID_FILE="${RL_SERVER_PID_FILE:-${RUNTIME_DIR}/rl-rollout-server.pid}"
RL_QUEUE_DIR="${RL_QUEUE_DIR:-${RUNTIME_DIR}/rl-queue}"
RL_ACTIVE_DIR="${RL_ACTIVE_DIR:-${RL_QUEUE_DIR}/active}"
RL_JOB_QUEUE_ROOT="${RL_JOB_QUEUE_ROOT:-${RL_QUEUE_DIR}/jobs}"
RL_JOB_RUNTIME_ROOT="${RL_JOB_RUNTIME_ROOT:-${RUNTIME_DIR}/rl-jobs}"
RL_DYNAMIC_JOB_ZELLIJ="${RL_DYNAMIC_JOB_ZELLIJ:-1}"
RL_JOB_ZELLIJ_START_TIMEOUT="${RL_JOB_ZELLIJ_START_TIMEOUT:-120}"
RL_JOB_ZELLIJ_LOCK_TIMEOUT="${RL_JOB_ZELLIJ_LOCK_TIMEOUT:-30}"
RL_JOB_ZELLIJ_READY_TIMEOUT="${RL_JOB_ZELLIJ_READY_TIMEOUT:-90}"
RL_WORKERS="${RL_WORKERS:-${RL_MAX_CONCURRENT:-$TOTAL_WORKERS}}"
RL_REQUEST_TIMEOUT="${RL_REQUEST_TIMEOUT:-3600}"
RL_KEEP_TRIALS_PER_WORKER="${RL_KEEP_TRIALS_PER_WORKER:-20}"
RL_MODEL_INFO="${RL_MODEL_INFO:-$TB_MODEL_INFO}"
RL_MAX_NEW_TOKENS="${RL_MAX_NEW_TOKENS:-}"
RL_CLAUDE_CODE_MAX_OUTPUT_TOKENS="${RL_CLAUDE_CODE_MAX_OUTPUT_TOKENS:-}"
RL_MAX_TURNS="${RL_MAX_TURNS:-32}"
RL_AGENT_TIMEOUT_MULTIPLIER="${RL_AGENT_TIMEOUT_MULTIPLIER:-$TB_AGENT_TIMEOUT_MULTIPLIER}"
RL_LLM_TIMEOUT="${RL_LLM_TIMEOUT:-900}"
RL_LLM_MAX_RETRIES="${RL_LLM_MAX_RETRIES:-0}"
RL_TEMPERATURE="${RL_TEMPERATURE:-1.0}"
RL_TOP_P="${RL_TOP_P:-1.0}"
RL_TOP_K="${RL_TOP_K:--1}"
RL_MIN_P="${RL_MIN_P:-0.0}"
RL_COLLECT_ROLLOUT_DETAILS="${RL_COLLECT_ROLLOUT_DETAILS:-true}"
RL_ENABLE_SUMMARIZE="${RL_ENABLE_SUMMARIZE:-false}"

# Harbor accepts either a built-in environment name or a module:Class import
# path. YiCloud uses its own OpenAPI SDK and OGW signing, so it is loaded as a
# provider adapter without patching the PyPI Harbor package.
TB_ENVIRONMENT_TYPE="${TB_ENVIRONMENT_TYPE:-$RL_ENVIRONMENT_TYPE}"
if [[ -z "${TB_ENVIRONMENT_SPEC:-}" ]]; then
  if [[ "$TB_ENVIRONMENT_TYPE" == "opensandbox" ]]; then
    TB_ENVIRONMENT_SPEC="yicloud_opensandbox:YiCloudOpenSandboxEnvironment"
  elif [[ "$TB_ENVIRONMENT_TYPE" == "e2b" && -n "$TB_E2B_PREBUILT_TEMPLATE" ]]; then
    TB_ENVIRONMENT_SPEC="$HARBOR_E2B_PREBUILT_ENVIRONMENT_SPEC"
  else
    TB_ENVIRONMENT_SPEC="$TB_ENVIRONMENT_TYPE"
  fi
fi
HARBOR_OPENSANDBOX_IMAGE_REF="${HARBOR_OPENSANDBOX_IMAGE_REF:-}"
HARBOR_OPENSANDBOX_REGISTRY="${HARBOR_OPENSANDBOX_REGISTRY:-registry.gate.yicloud.com.cn}"
HARBOR_OPENSANDBOX_IMAGE_REPOSITORY="${HARBOR_OPENSANDBOX_IMAGE_REPOSITORY:-${YICLOUD_PROJECT_NAME:+${YICLOUD_PROJECT_NAME}/syslab-benchmark-task-images}}"
HARBOR_OPENSANDBOX_SANDBOX_IMAGE_PREFIX="${HARBOR_OPENSANDBOX_SANDBOX_IMAGE_PREFIX:-$HARBOR_OPENSANDBOX_IMAGE_REPOSITORY}"
HARBOR_OPENSANDBOX_DOCKER_CONFIG="${HARBOR_OPENSANDBOX_DOCKER_CONFIG:-$HOME/.docker/config.json}"
HARBOR_OPENSANDBOX_IMAGE_CACHE_ROOT="${HARBOR_OPENSANDBOX_IMAGE_CACHE_ROOT:-/data/harbor-runs/opensandbox-images}"
HARBOR_OPENSANDBOX_IMAGE_PLATFORM="${HARBOR_OPENSANDBOX_IMAGE_PLATFORM:-linux/amd64}"
HARBOR_OPENSANDBOX_IMAGE_TAG_PREFIX="${HARBOR_OPENSANDBOX_IMAGE_TAG_PREFIX:-harbor}"
HARBOR_OPENSANDBOX_DOCKERHUB_MIRROR_PREFIX="${HARBOR_OPENSANDBOX_DOCKERHUB_MIRROR_PREFIX:-m.daocloud.io/docker.io}"
HARBOR_OPENSANDBOX_APT_MIRROR="${HARBOR_OPENSANDBOX_APT_MIRROR:-http://mirrors.tuna.tsinghua.edu.cn}"
HARBOR_OPENSANDBOX_BUILD_ARGS_JSON="${HARBOR_OPENSANDBOX_BUILD_ARGS_JSON:-}"
if [[ -z "${HARBOR_OPENSANDBOX_BUILD_ARGS_JSON}" ]]; then
  HARBOR_OPENSANDBOX_BUILD_ARGS_JSON='{}'
fi
HARBOR_OPENSANDBOX_BUILD_USE_PROXY="${HARBOR_OPENSANDBOX_BUILD_USE_PROXY:-0}"
HARBOR_OPENSANDBOX_IMAGE_MANAGER="${HARBOR_OPENSANDBOX_IMAGE_MANAGER:-$SCRIPT_DIR/opensandbox_image_manager.py}"

export SCRIPT_DIR REPO_ROOT AGENTS_DIR TASKS_DIR HARBOR_CLAUDE_CODE_DIR HARBOR_OPENCODE_DIR WORKSPACE_DIR RUN_ID TOTAL_WORKERS N_ATTEMPTS MODEL AGENT MAX_RETRIES
export HARBOR_ROOT DATASET_PATH DATASET_NAME METRIC_MODE OUTPUT_ROOT OUTPUT_PATH TASK_SOURCE_FILE TASK_FILE FLEET_TASKS QUEUE_DIR RUNTIME_DIR LAYOUT_FILE JOBS_ROOT
export HARBOR_ONLINE_ANALYSIS HARBOR_ONLINE_ANALYSIS_POLL_INTERVAL HARBOR_ONLINE_ANALYSIS_DIR HARBOR_ONLINE_ANALYSIS_PID_FILE HARBOR_ONLINE_ANALYSIS_LOG_FILE HARBOR_EARLY_STOP HARBOR_ZELLIJ_CLOSE_ON_COMPLETE HARBOR_ZELLIJ_KEEP_ON_FAILURE
export HARBOR_MONITOR_ENABLED HARBOR_MONITOR_DIR HARBOR_MONITOR_PID_FILE HARBOR_MONITOR_LOG_FILE HARBOR_BENCHMARK_PID_FILE HARBOR_BENCHMARK_EXIT_FILE HARBOR_JOB_DIR_FILE HARBOR_MONITOR_RESTART_CMD HARBOR_MONITOR_STOP_CMD HARBOR_MONITOR_INTERVAL HARBOR_MONITOR_STARTUP_GRACE HARBOR_MONITOR_STALL_SECONDS HARBOR_MONITOR_MAX_RETRIES HARBOR_MONITOR_CONFIGURED_TIMEOUT
export API_KEY BASE_URL HARBOR_ANALYZER_API_KEY HARBOR_ANALYZER_BASE_URL HARBOR_ANALYZER_MODEL HARBOR_ANALYZER_PI_PROVIDER HARBOR_ANALYZER_NO_PROXY HARBOR_ANALYZER_ENABLED HARBOR_ANALYZER_MODE HARBOR_ANALYZER_OUTPUT_DIR HARBOR_ANALYZER_PID_FILE HARBOR_ANALYZER_SUPERVISOR_PID_FILE HARBOR_ANALYZER_SUPERVISOR_ID_FILE HARBOR_ANALYZER_LOG_FILE HARBOR_ANALYZER_POLL_INTERVAL HARBOR_ANALYZER_TIMEOUT HARBOR_ANALYZER_MAX_CONCURRENCY TRACE_TO_OPIK OPIK_URL OPIK_URL_OVERRIDE OPIK_BASE OPIK_MODE OPIK_PROJECT_NAME OPIK_API_KEY OPIK_WORKSPACE CC_OPIK_DEBUG
export HARBOR_RUN_TIMESTAMP HARBOR_SESSION_TIMESTAMP HARBOR_RUN_AGENT_NAME HARBOR_RUN_DATASET_NAME HARBOR_RUN_MODEL_NAME HARBOR_ZELLIJ_SESSION_NAME
export CLAUDE_CODE_VERSION CLAUDE_CODE_TGZ_BASENAME LOCAL_WHEEL_DIR LOCAL_WHEEL_PORT LOCAL_WHEEL_PORT_ATTEMPTS LOCAL_WHEEL_HOST_IP
export TB_LOCAL_WHEEL_SERVER_URL TB_LOCAL_CLAUDE_TGZ_URL TB_REMOTE_WHEEL_SERVER_URLS EFFECTIVE_WHEEL_URL_FILE EFFECTIVE_CLAUDE_TGZ_URL_FILE LOCAL_DEPS_LOG_FILE HARBOR_RUNNER_PREPARE HARBOR_RUNNER_IMAGE_DIR HARBOR_RUNNER_HOST_DIR HARBOR_RUNNER_PYTHON_VERSION HARBOR_RUNNER_DIR HARBOR_OPIK_BIN HARBOR_CLI_BIN HARBOR_OPIK_PYTHON HARBOR_RUNNER_REQUIREMENTS HARBOR_RUNNER_PREPARE_STATUS_FILE HARBOR_RUNNER_PREPARE_LOG_FILE
export TB_DATASET_GIT_URL TB_PATH TB_LIMIT TB_RUNS TB_AGENT TB_AGENT_IMPORT_PATH TB_MODEL INCLUDE_TASKS TB_DRY_RUN MIN_TEST MIN_TEST_INCLUDE_TASK
export TB_ENVIRONMENT_TYPE TB_ENVIRONMENT_SPEC HARBOR_OPENSANDBOX_IMAGE_REF HARBOR_OPENSANDBOX_REGISTRY HARBOR_OPENSANDBOX_IMAGE_REPOSITORY HARBOR_OPENSANDBOX_SANDBOX_IMAGE_PREFIX HARBOR_OPENSANDBOX_DOCKER_CONFIG HARBOR_OPENSANDBOX_IMAGE_CACHE_ROOT HARBOR_OPENSANDBOX_IMAGE_PLATFORM HARBOR_OPENSANDBOX_IMAGE_TAG_PREFIX HARBOR_OPENSANDBOX_DOCKERHUB_MIRROR_PREFIX HARBOR_OPENSANDBOX_APT_MIRROR HARBOR_OPENSANDBOX_BUILD_ARGS_JSON HARBOR_OPENSANDBOX_BUILD_USE_PROXY HARBOR_OPENSANDBOX_IMAGE_MANAGER
export TB_E2B_SANDBOX_TIMEOUT_SEC TB_E2B_PREBUILT_TEMPLATE HARBOR_E2B_PREBUILT_ENVIRONMENT_SPEC
export TB_N_CONCURRENT TB_MAX_RETRIES TB_RETRY_INCLUDE_EXCEPTIONS TB_RETRY_EXCLUDE_EXCEPTIONS TB_AK_MAX_TURNS TB_AK_COLLECT_ROLLOUT_DETAILS TB_AK_ENABLE_SUMMARIZE TB_DISALLOWED_TOOLS TB_APPEND_SYSTEM_PROMPT
export HARBOR_TEMPERATURE HARBOR_TOP_P HARBOR_MAX_TOKENS
export TB_API_BASE TB_LLM_KWARGS TB_MAX_NEW_TOKENS TB_MODEL_INFO TB_ANTHROPIC_BASE_URL TB_ANTHROPIC_AUTH_TOKEN TB_ANTHROPIC_CUSTOM_HEADERS TB_CLAUDE_CODE_MAX_OUTPUT_TOKENS
export TB_CLAUDE_CODE_DISABLE_AUTOUPDATER TB_ANTHROPIC_MODEL TB_ANTHROPIC_DEFAULT_OPUS_MODEL TB_ANTHROPIC_DEFAULT_SONNET_MODEL TB_ANTHROPIC_DEFAULT_HAIKU_MODEL TB_CLAUDE_CODE_SUBAGENT_MODEL TB_CLAUDE_CODE_EFFORT_LEVEL
export TB_TIMEOUT_MULTIPLIER TB_AGENT_TIMEOUT_MULTIPLIER TB_AGENT_SETUP_TIMEOUT_MULTIPLIER TB_FORCE_BUILD TB_DEBUG TRACE_PLUGIN_SOURCE_DIR TRACE_PLUGIN_CLAUDE_HOOK_SOURCE TRACE_PLUGIN_OPENCODE_PLUGIN_SOURCE TRACE_PLUGIN_OPENCODE_HOOK_SOURCE TB_CC_OPIK_ENABLE_HOOK
export TB_CC_HOOK_SOURCE TB_CC_HOOK_MOUNT_PATH TB_CC_CLAUDE_TGZ_SOURCE TB_CC_CLAUDE_TGZ_MOUNT_PATH
export TB_CC_PY_WHEEL_DIR_SOURCE TB_CC_PY_WHEEL_DIR_MOUNT_PATH TB_CC_NPM_CACHE_MOUNT_PATH TB_VERIFIER_UV_HOME TB_VERIFIER_UV_BIN_DIR_MOUNT_PATH TB_E2B_VERIFIER_UV_SOURCE TB_CC_OPIK_DEBUG TB_CC_OPIK_INSTALL_DEPS
export NPM_CONFIG_REGISTRY GO111MODULE GOPROXY GOSUMDB
export RUSTUP_UPDATE_ROOT RUSTUP_DIST_SERVER CARGO_REGISTRY_REPLACE_WITH CARGO_REGISTRY_URL
export PIP_INDEX_URL PIP_EXTRA_INDEX_URL PIP_TRUSTED_HOST UV_INDEX_URL UV_DEFAULT_INDEX
export TB_PIP_DEFAULT_TIMEOUT TB_PIP_RETRIES
export OPIK_REPO_DIR COMPOSE_DIR TB_SKIP_DOCKERHUB_PREFLIGHT TB_DOCKERHUB_CHECK_TIMEOUT TB_DOCKERHUB_PREFLIGHT_STRICT SMITH_GENERATE_IF_MISSING SMITH_ADAPTER_DIR FIX_GIT_IMAGE_NAME FIX_GIT_WARM_LABEL
export OPENCODE_PROVIDER OPENCODE_VERSION OPENCODE_TGZ_BASENAME OPENCODE_LINUX_X64_TGZ_BASENAME OPENCODE_CONFIG_CONTENT
export NEXT_INDEX_FILE LOCK_FILE WORKERS_READY_FILE WORKERS_FAILED_FILE
export ROLLOUT RL_UTILS_DIR RL_ENV_FILE RL_HOST RL_PORT RL_DATASET_NAME RL_DATASET_ROOT RL_DATASET_ROOTS RL_TRIALS_DIR RL_MAX_CONCURRENT
export RL_AGENT RL_MODEL_NAME RL_MODEL_PREFIX RL_API_BASE RL_API_KEY RL_API_KEY_MODE RL_DISABLED_TASK_IDS RL_ENVIRONMENT_TYPE RL_E2B_SANDBOX_TIMEOUT_SEC RL_E2B_PREBUILT_TEMPLATE RL_FORCE_BUILD
export RL_TRACE_LOG RL_SERVER_LOG RL_SERVER_PID_FILE RL_QUEUE_DIR RL_ACTIVE_DIR RL_JOB_QUEUE_ROOT RL_JOB_RUNTIME_ROOT RL_DYNAMIC_JOB_ZELLIJ RL_JOB_ZELLIJ_START_TIMEOUT RL_JOB_ZELLIJ_LOCK_TIMEOUT RL_JOB_ZELLIJ_READY_TIMEOUT RL_WORKERS RL_REQUEST_TIMEOUT RL_KEEP_TRIALS_PER_WORKER RL_MODEL_INFO RL_MAX_NEW_TOKENS RL_CLAUDE_CODE_MAX_OUTPUT_TOKENS RL_MAX_TURNS RL_AGENT_TIMEOUT_MULTIPLIER RL_LLM_TIMEOUT RL_LLM_MAX_RETRIES
export RL_TEMPERATURE RL_TOP_P RL_TOP_K RL_MIN_P RL_COLLECT_ROLLOUT_DETAILS RL_ENABLE_SUMMARIZE
export PATH="/opt/tb-venv/bin:${PATH}"

harbor_init_run_dirs() {
  mkdir -p "$OUTPUT_PATH" "$QUEUE_DIR" "$RUNTIME_DIR/worker-logs" "$JOBS_ROOT"
  touch "$QUEUE_DIR/done.txt" "$QUEUE_DIR/failed.txt"
}

harbor_agent_is_opencode() {
  [[ "$AGENT" == "opencode" ]]
}

harbor_agent_is_claude_code() {
  [[ "$AGENT" == "claude-code" ]]
}

harbor_validate_generation_controls() {
  if [[ -n "$HARBOR_MAX_TOKENS" && ! "$HARBOR_MAX_TOKENS" =~ ^[1-9][0-9]*$ ]]; then
    echo "[ERROR] HARBOR_MAX_TOKENS must be a positive integer." >&2
    return 1
  fi
  if [[ "$ROLLOUT" != "1" ]] \
    && harbor_agent_is_claude_code \
    && [[ -n "$HARBOR_TEMPERATURE" || -n "$HARBOR_TOP_P" ]]; then
    echo "[ERROR] Claude Code does not expose temperature or top_p controls." >&2
    echo "[ERROR] Use AGENT=opencode for these settings, or leave them unset." >&2
    return 1
  fi
}

harbor_analyzer_pid_matches_run() {
  local pid="$1" arg previous="" script_seen=0 run_seen=0 handover_seen=0
  [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/cmdline" ]] || return 1
  while IFS= read -r -d '' arg; do
    [[ "$arg" == "$SCRIPT_DIR/scripts/analyzer_subagent.py" ]] && script_seen=1
    [[ "$previous" == "--run-dir" && "$arg" == "$OUTPUT_PATH" ]] && run_seen=1
    [[ "$previous" == "--handover" && "$arg" == "$HARBOR_MONITOR_DIR/analyzer-handover-latest.json" ]] && handover_seen=1
    previous="$arg"
  done < "/proc/$pid/cmdline"
  [[ "$script_seen" == 1 && "$run_seen" == 1 && "$handover_seen" == 1 ]]
}

harbor_process_start_time() {
  local pid="$1"
  [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/stat" ]] || return 1
  awk '{print $22}' "/proc/$pid/stat" 2>/dev/null
}

harbor_identity_file_value() {
  local file="$1" key="$2" line_key line_value
  [[ -f "$file" ]] || return 1
  while IFS='=' read -r line_key line_value; do
    if [[ "$line_key" == "$key" ]]; then
      printf '%s\n' "$line_value"
      return 0
    fi
  done < "$file"
  return 1
}

harbor_write_analyzer_supervisor_identity() {
  local pid="$1" analyzer_pid="$2" start_time analyzer_start_time
  start_time="$(harbor_process_start_time "$pid")" || return 1
  analyzer_start_time="$(harbor_process_start_time "$analyzer_pid")" || return 1
  {
    printf 'pid=%s\n' "$pid"
    printf 'start_time=%s\n' "$start_time"
    printf 'run_dir=%s\n' "$OUTPUT_PATH"
    printf 'analyzer_pid=%s\n' "$analyzer_pid"
    printf 'analyzer_start_time=%s\n' "$analyzer_start_time"
  } > "$HARBOR_ANALYZER_SUPERVISOR_ID_FILE"
}

harbor_analyzer_supervisor_pid_matches_run() {
  local pid="$1" expected_analyzer_pid="${2:-}" stored_pid start_time run_dir analyzer_pid analyzer_start_time
  local current_start_time current_analyzer_start_time
  [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/stat" ]] || return 1
  stored_pid="$(harbor_identity_file_value "$HARBOR_ANALYZER_SUPERVISOR_ID_FILE" pid || true)"
  start_time="$(harbor_identity_file_value "$HARBOR_ANALYZER_SUPERVISOR_ID_FILE" start_time || true)"
  run_dir="$(harbor_identity_file_value "$HARBOR_ANALYZER_SUPERVISOR_ID_FILE" run_dir || true)"
  analyzer_pid="$(harbor_identity_file_value "$HARBOR_ANALYZER_SUPERVISOR_ID_FILE" analyzer_pid || true)"
  analyzer_start_time="$(harbor_identity_file_value "$HARBOR_ANALYZER_SUPERVISOR_ID_FILE" analyzer_start_time || true)"
  [[ "$stored_pid" == "$pid" && "$run_dir" == "$OUTPUT_PATH" ]] || return 1
  current_start_time="$(harbor_process_start_time "$pid")" || return 1
  [[ -n "$start_time" && "$start_time" == "$current_start_time" ]] || return 1
  [[ -z "$expected_analyzer_pid" || "$analyzer_pid" == "$expected_analyzer_pid" ]] || return 1
  if [[ -n "$expected_analyzer_pid" ]]; then
    [[ "$analyzer_pid" =~ ^[0-9]+$ && -r "/proc/$analyzer_pid/stat" ]] || return 1
    current_analyzer_start_time="$(harbor_process_start_time "$analyzer_pid")" || return 1
    [[ -n "$analyzer_start_time" && "$analyzer_start_time" == "$current_analyzer_start_time" ]] || return 1
    harbor_analyzer_pid_matches_run "$analyzer_pid"
  fi
}

harbor_agent_is_oracle() {
  [[ "$AGENT" == "oracle" ]]
}

harbor_validate_agent() {
  case "$AGENT" in
    claude-code|opencode|oracle) ;;
    *)
      echo "[ERROR] AGENT must be claude-code, opencode, or oracle, got: $AGENT" >&2
      exit 1
      ;;
  esac

  if harbor_agent_is_opencode; then
    if [[ -z "$OPENCODE_CONFIG_CONTENT" ]]; then
      echo "[WARN] AGENT=opencode but OPENCODE_CONFIG_CONTENT is empty;" >&2
      echo "[WARN] opencode will fall back to ANTHROPIC_* env if provided." >&2
    fi
  fi
}

harbor_monitor_pid_matches_run() {
  local pid="$1" arg previous="" script_seen=0 run_seen=0
  [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/cmdline" ]] || return 1
  while IFS= read -r -d '' arg; do
    [[ "$arg" == "$SCRIPT_DIR/scripts/monitor.py" ]] && script_seen=1
    [[ "$previous" == "--run-dir" && "$arg" == "$OUTPUT_PATH" ]] && run_seen=1
    previous="$arg"
  done < "/proc/$pid/cmdline"
  [[ "$script_seen" == 1 && "$run_seen" == 1 ]]
}

harbor_online_analysis_pid_matches_run() {
  local pid="$1" arg script_seen=0 run_seen=0
  [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/cmdline" ]] || return 1
  while IFS= read -r -d '' arg; do
    [[ "$arg" == "$SCRIPT_DIR/scripts/online_rule_analyzer.py" ]] && script_seen=1
    [[ "$arg" == "$OUTPUT_PATH" ]] && run_seen=1
  done < "/proc/$pid/cmdline"
  [[ "$script_seen" == 1 && "$run_seen" == 1 ]]
}

harbor_stop_online_analysis() {
  [[ -f "$HARBOR_ONLINE_ANALYSIS_PID_FILE" ]] || return 0
  local pid sid signal_target
  pid="$(cat "$HARBOR_ONLINE_ANALYSIS_PID_FILE" 2>/dev/null || true)"
  if [[ ! "$pid" =~ ^[0-9]+$ ]] || ! kill -0 "$pid" >/dev/null 2>&1; then
    rm -f "$HARBOR_ONLINE_ANALYSIS_PID_FILE"
    return 0
  fi
  if ! harbor_online_analysis_pid_matches_run "$pid"; then
    echo "[ERROR] refusing to stop unrelated process from $HARBOR_ONLINE_ANALYSIS_PID_FILE: pid=$pid" >&2
    return 1
  fi
  sid="$(ps -o sid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
  signal_target="$pid"
  [[ "$sid" == "$pid" ]] && signal_target="-$pid"
  kill -TERM -- "$signal_target" >/dev/null 2>&1 || true
  for _ in $(seq 1 30); do
    kill -0 "$pid" >/dev/null 2>&1 || break
    sleep 1
  done
  if kill -0 "$pid" >/dev/null 2>&1; then
    kill -KILL -- "$signal_target" >/dev/null 2>&1 || true
  fi
  rm -f "$HARBOR_ONLINE_ANALYSIS_PID_FILE"
}

harbor_stop_monitor() {
  [[ -f "$HARBOR_MONITOR_PID_FILE" ]] || return 0
  local pid sid signal_target
  pid="$(cat "$HARBOR_MONITOR_PID_FILE" 2>/dev/null || true)"
  if [[ ! "$pid" =~ ^[0-9]+$ ]] || ! kill -0 "$pid" >/dev/null 2>&1; then
    rm -f "$HARBOR_MONITOR_PID_FILE"
    return 0
  fi
  if ! harbor_monitor_pid_matches_run "$pid"; then
    echo "[ERROR] refusing to stop unrelated process from $HARBOR_MONITOR_PID_FILE: pid=$pid" >&2
    return 1
  fi
  sid="$(ps -o sid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
  signal_target="$pid"
  [[ "$sid" == "$pid" ]] && signal_target="-$pid"
  kill -TERM -- "$signal_target" >/dev/null 2>&1 || true
  for _ in $(seq 1 30); do
    kill -0 "$pid" >/dev/null 2>&1 || break
    sleep 1
  done
  if kill -0 "$pid" >/dev/null 2>&1; then
    kill -KILL -- "$signal_target" >/dev/null 2>&1 || true
  fi
  rm -f "$HARBOR_MONITOR_PID_FILE"
}

harbor_reset_run_state() {
  harbor_stop_analyzer_supervisor
  harbor_stop_analyzer
  harbor_stop_monitor
  harbor_stop_online_analysis
  rm -f "$QUEUE_DIR"/worker-*.current "$LOCK_FILE" "$WORKERS_READY_FILE" "$WORKERS_FAILED_FILE"
  rm -f "$NEXT_INDEX_FILE"
  rm -f "$OUTPUT_PATH/.monitor_state.json" "$HARBOR_MONITOR_LOG_FILE" "$HARBOR_BENCHMARK_PID_FILE" "$HARBOR_BENCHMARK_EXIT_FILE" "$HARBOR_JOB_DIR_FILE"
  rm -rf "$HARBOR_MONITOR_DIR"
  rm -f "$HARBOR_ANALYZER_PID_FILE" "$HARBOR_ANALYZER_SUPERVISOR_PID_FILE" "$HARBOR_ANALYZER_SUPERVISOR_ID_FILE" "$HARBOR_ANALYZER_LOG_FILE" "$HARBOR_ANALYZER_OUTPUT_DIR/.analyzer_state.json" "$HARBOR_ANALYZER_OUTPUT_DIR/.analyzer-ready" "$HARBOR_ANALYZER_OUTPUT_DIR/analyzer-artifacts-latest.json" "$HARBOR_ANALYZER_OUTPUT_DIR/benchmark-summary.md"
  rm -rf "$HARBOR_ANALYZER_OUTPUT_DIR/benchmark-summary"
  rm -f "$HARBOR_ONLINE_ANALYSIS_PID_FILE" "$HARBOR_ONLINE_ANALYSIS_LOG_FILE"
  rm -f "$HARBOR_ONLINE_ANALYSIS_DIR/environment-events.jsonl" "$HARBOR_ONLINE_ANALYSIS_DIR/environment-summary.json"
  : > "$QUEUE_DIR/done.txt"
  : > "$QUEUE_DIR/failed.txt"
}

harbor_stop_analyzer_supervisor() {
  [[ -f "$HARBOR_ANALYZER_SUPERVISOR_PID_FILE" ]] || return 0
  local pid
  pid="$(cat "$HARBOR_ANALYZER_SUPERVISOR_PID_FILE" 2>/dev/null || true)"
  if [[ ! "$pid" =~ ^[0-9]+$ ]] || ! kill -0 "$pid" >/dev/null 2>&1; then
    rm -f "$HARBOR_ANALYZER_SUPERVISOR_PID_FILE" "$HARBOR_ANALYZER_SUPERVISOR_ID_FILE"
    return 0
  fi
  if ! harbor_analyzer_supervisor_pid_matches_run "$pid"; then
    echo "[ERROR] refusing to stop unrelated process from $HARBOR_ANALYZER_SUPERVISOR_PID_FILE: pid=$pid" >&2
    return 1
  fi
  kill -TERM "$pid" >/dev/null 2>&1 || true
  for _ in $(seq 1 10); do
    kill -0 "$pid" >/dev/null 2>&1 || break
    sleep 1
  done
  if kill -0 "$pid" >/dev/null 2>&1; then
    kill -KILL "$pid" >/dev/null 2>&1 || true
  fi
  rm -f "$HARBOR_ANALYZER_SUPERVISOR_PID_FILE" "$HARBOR_ANALYZER_SUPERVISOR_ID_FILE"
}

harbor_stop_analyzer() {
  [[ -f "$HARBOR_ANALYZER_PID_FILE" ]] || return 0
  local expected_pid="${1:-}" current_pid pid sid signal_target
  current_pid="$(cat "$HARBOR_ANALYZER_PID_FILE" 2>/dev/null || true)"
  pid="${expected_pid:-$current_pid}"
  if [[ -n "$expected_pid" && "$current_pid" != "$expected_pid" ]]; then
    [[ "$expected_pid" =~ ^[0-9]+$ ]] || return 0
  fi
  if [[ ! "$pid" =~ ^[0-9]+$ ]] || ! kill -0 "$pid" >/dev/null 2>&1; then
    [[ -z "$expected_pid" || "$current_pid" == "$expected_pid" ]] && rm -f "$HARBOR_ANALYZER_PID_FILE"
    return 0
  fi
  if ! harbor_analyzer_pid_matches_run "$pid"; then
    echo "[ERROR] refusing to stop unrelated process from $HARBOR_ANALYZER_PID_FILE: pid=$pid" >&2
    return 1
  fi
  sid="$(ps -o sid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
  signal_target="$pid"
  [[ "$sid" == "$pid" ]] && signal_target="-$pid"
  kill -TERM -- "$signal_target" >/dev/null 2>&1 || true
  for _ in $(seq 1 30); do
    kill -0 "$pid" >/dev/null 2>&1 || break
    sleep 1
  done
  if kill -0 "$pid" >/dev/null 2>&1; then
    kill -KILL -- "$signal_target" >/dev/null 2>&1 || true
  fi
  [[ -z "$expected_pid" || "$current_pid" == "$expected_pid" ]] && rm -f "$HARBOR_ANALYZER_PID_FILE"
}

harbor_generate_task_file() {
  local destination="${1:-$TASK_FILE}" source_file=""
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

harbor_uses_registry_dataset() {
  harbor_registry_dataset_name >/dev/null
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
  INCLUDE_TASKS="$FLEET_TASKS"
  TB_INCLUDE_TASKS="$FLEET_TASKS"
  export INCLUDE_TASKS TB_INCLUDE_TASKS
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
}

harbor_task_count() {
  if [[ -f "$TASK_FILE" ]]; then
    wc -l < "$TASK_FILE" | tr -d ' '
  else
    echo 0
  fi
}

harbor_ensure_dataset() {
  local dataset_kind
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
    return 1
  fi
}

harbor_pick_task() {
  exec 9>"$LOCK_FILE"
  flock 9

  local total idx task_name
  total="$(harbor_task_count)"
  idx="$(cat "$NEXT_INDEX_FILE" 2>/dev/null || echo 1)"
  if [[ -z "$idx" || "$idx" -gt "$total" ]]; then
    flock -u 9
    return 1
  fi

  task_name="$(sed -n "${idx}p" "$TASK_FILE" | tr -d '\r')"
  echo $((idx + 1)) > "$NEXT_INDEX_FILE"
  flock -u 9

  [[ -n "$task_name" ]] || return 1
  printf '%s\t%s\n' "$idx" "$task_name"
}

harbor_wait_for_workers_ready() {
  while true; do
    if [[ -f "$WORKERS_READY_FILE" ]]; then
      harbor_apply_effective_wheel_source
      return 0
    fi
    [[ -f "$WORKERS_FAILED_FILE" ]] && return 1
    sleep 1
  done
}

harbor_ensure_local_wheels_server() {
  mkdir -p "$RUNTIME_DIR"
  local pid_file="${RUNTIME_DIR}/local-wheel-http.pid"
  local log_file="${RUNTIME_DIR}/local-wheel-http.log"
  local port pid attempt last_port

  [[ -d "$LOCAL_WHEEL_DIR" ]] || return 0

  last_port=$((LOCAL_WHEEL_PORT + LOCAL_WHEEL_PORT_ATTEMPTS - 1))
  for port in $(seq "$LOCAL_WHEEL_PORT" "$last_port"); do
    export TB_LOCAL_WHEEL_SERVER_URL="http://${LOCAL_WHEEL_HOST_IP}:${port}"
    export TB_LOCAL_CLAUDE_TGZ_URL="${TB_LOCAL_WHEEL_SERVER_URL%/}/${CLAUDE_CODE_TGZ_BASENAME}"
    local agent_tgz="$CLAUDE_CODE_TGZ_BASENAME"
    if harbor_agent_is_opencode; then
      agent_tgz="$OPENCODE_TGZ_BASENAME"
    fi

    # Treat wheel servers without the selected agent tgz as incomplete.
    local urls=("${TB_LOCAL_WHEEL_SERVER_URL%/}/manifest.txt" "${TB_LOCAL_WHEEL_SERVER_URL%/}/${agent_tgz}")
    if harbor_agent_is_opencode; then
      urls+=("${TB_LOCAL_WHEEL_SERVER_URL%/}/${OPENCODE_LINUX_X64_TGZ_BASENAME}")
    else
      urls+=("${TB_LOCAL_WHEEL_SERVER_URL%/}/npm-cache-ready")
    fi

    # Avoid probing a broad range of ports. Start on the preferred port first;
    # only if binding fails do a narrow readiness check to see whether another
    # monitor already owns a compatible wheel server on that exact port.
    nohup python3 -m http.server "$port" --directory "$LOCAL_WHEEL_DIR" \
      >"$log_file.${port}" 2>&1 &
    pid="$!"
    sleep 1

    local ready=1
    local url
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      ready=1
      for url in "${urls[@]}"; do
        if ! harbor_url_is_reachable "$url"; then
          ready=0
          break
        fi
      done
      if [[ "$ready" == "1" ]]; then
        echo "$port" > "${RUNTIME_DIR}/local-wheel-http.port"
        return 0
      fi
      continue
    fi

    for url in "${urls[@]}"; do
      if ! harbor_url_is_reachable "$url"; then
        ready=0
        break
      fi
    done
    if [[ "$ready" == "1" ]]; then
      echo "$pid" > "$pid_file"
      echo "$port" > "${RUNTIME_DIR}/local-wheel-http.port"
      return 0
    fi
    kill "$pid" >/dev/null 2>&1 || true
  done

  echo "failed to start a matching local wheel HTTP server in ${LOCAL_WHEEL_PORT_ATTEMPTS} attempts" >&2
  return 1
}

harbor_url_is_reachable() {
  local url="$1"
  python3 "$SCRIPT_DIR/env.py" url-reachable "$url"
}

harbor_manifest_url_ready() {
  local url="$1"
  python3 "$SCRIPT_DIR/env.py" manifest-url-ready "$url"
}

harbor_gzip_file_ready() {
  local path="$1"
  [[ -f "$path" ]] && gzip -t "$path" >/dev/null 2>&1
}

harbor_tar_file_ready() {
  local path="$1"
  python3 "$SCRIPT_DIR/env.py" tar-file-ready "$path" >/dev/null 2>&1
}

harbor_local_cache_ready() {
  [[ -f "$LOCAL_WHEEL_DIR/manifest.txt" ]] \
    && grep -qx 'cache_schema=3' "$LOCAL_WHEEL_DIR/manifest.txt" \
    && [[ "$(find "$LOCAL_WHEEL_DIR" -maxdepth 1 -name 'opik-*.whl' -type f | wc -l | tr -d ' ')" == "1" ]] \
    && [[ -f "$LOCAL_WHEEL_DIR/get-pip.py" ]] \
    && harbor_tar_file_ready "$LOCAL_WHEEL_DIR/node-runtime.tar.xz" \
    && harbor_gzip_file_ready "$LOCAL_WHEEL_DIR/python3.12-runtime.tar.gz" \
    && {
      if harbor_agent_is_opencode; then
        harbor_gzip_file_ready "$LOCAL_WHEEL_DIR/${OPENCODE_TGZ_BASENAME}" \
          && harbor_gzip_file_ready "$LOCAL_WHEEL_DIR/${OPENCODE_LINUX_X64_TGZ_BASENAME}"
      else
        [[ -f "$LOCAL_WHEEL_DIR/${CLAUDE_CODE_TGZ_BASENAME}" ]] \
          && [[ -d "$LOCAL_WHEEL_DIR/npm-cache/_cacache" ]] \
          && [[ -f "$LOCAL_WHEEL_DIR/npm-cache-ready" ]] \
          && grep -qx "claude_npm_cache_version=${CLAUDE_CODE_VERSION}" "$LOCAL_WHEEL_DIR/manifest.txt"
      fi
    }
}

harbor_pick_remote_wheel_url() {
  local candidates=()
  local candidate
  if [[ -n "${TB_REMOTE_WHEEL_SERVER_URLS:-}" ]]; then
    IFS=',' read -r -a candidates <<< "$TB_REMOTE_WHEEL_SERVER_URLS"
  elif [[ -n "${TB_LOCAL_WHEEL_SERVER_URL:-}" ]]; then
    candidates=("$TB_LOCAL_WHEEL_SERVER_URL")
  fi

  for candidate in "${candidates[@]}"; do
    candidate="${candidate%% }"
    candidate="${candidate## }"
    [[ -n "${candidate:-}" ]] || continue
    local agent_tgz="$CLAUDE_CODE_TGZ_BASENAME"
    if harbor_agent_is_opencode; then
      agent_tgz="$OPENCODE_TGZ_BASENAME"
    fi
    local urls=("${candidate%/}/${agent_tgz}")
    if harbor_agent_is_opencode; then
      urls+=("${candidate%/}/${OPENCODE_LINUX_X64_TGZ_BASENAME}")
    else
      urls+=("${candidate%/}/npm-cache-ready")
    fi
    local ready=1
    local url
    if ! harbor_manifest_url_ready "${candidate%/}/manifest.txt"; then
      ready=0
    fi
    for url in "${urls[@]}"; do
      if ! harbor_url_is_reachable "$url"; then
        ready=0
        break
      fi
    done
    if [[ "$ready" == "1" ]]; then
      printf '%s\n' "${candidate%/}"
      return 0
    fi
  done
  return 1
}

harbor_write_effective_wheel_source() {
  local wheel_url="$1"
  printf '%s\n' "$wheel_url" > "$EFFECTIVE_WHEEL_URL_FILE"
  printf '%s\n' "${wheel_url%/}/${CLAUDE_CODE_TGZ_BASENAME}" > "$EFFECTIVE_CLAUDE_TGZ_URL_FILE"
  export TB_LOCAL_WHEEL_SERVER_URL="$wheel_url"
  export TB_LOCAL_CLAUDE_TGZ_URL="${wheel_url%/}/${CLAUDE_CODE_TGZ_BASENAME}"
}

harbor_apply_effective_wheel_source() {
  if [[ "$TB_ENVIRONMENT_TYPE" == "e2b" ]]; then
    # Public E2B Sandboxes cannot consume a runner-local HTTP server. Leave
    # these unset so agent installation uses a Sandbox-reachable registry.
    unset TB_LOCAL_WHEEL_SERVER_URL TB_LOCAL_CLAUDE_TGZ_URL
    return 0
  fi
  if [[ -f "$EFFECTIVE_WHEEL_URL_FILE" ]]; then
    export TB_LOCAL_WHEEL_SERVER_URL="$(cat "$EFFECTIVE_WHEEL_URL_FILE")"
  fi
  if [[ -f "$EFFECTIVE_CLAUDE_TGZ_URL_FILE" ]]; then
    export TB_LOCAL_CLAUDE_TGZ_URL="$(cat "$EFFECTIVE_CLAUDE_TGZ_URL_FILE")"
  elif [[ -n "${TB_LOCAL_WHEEL_SERVER_URL:-}" ]]; then
    export TB_LOCAL_CLAUDE_TGZ_URL="${TB_LOCAL_WHEEL_SERVER_URL%/}/${CLAUDE_CODE_TGZ_BASENAME}"
  fi
}

harbor_prewarm_s3_upload_cache() {
  if [[ "$TB_ENVIRONMENT_TYPE" != "opensandbox" \
    || "$YICLOUD_SANDBOX_UPLOAD_BACKEND" != "s3" ]]; then
    return 0
  fi
  if [[ ! -d "$LOCAL_WHEEL_DIR" ]]; then
    echo "S3 upload requires a prepared local dependency cache: $LOCAL_WHEEL_DIR" >&2
    return 1
  fi

  local -a sources=("$LOCAL_WHEEL_DIR")
  case "$AGENT" in
    claude-code)
      [[ -f "$TB_CC_CLAUDE_TGZ_SOURCE" ]] \
        && sources+=("$TB_CC_CLAUDE_TGZ_SOURCE")
      [[ -f "$TB_CC_HOOK_SOURCE" ]] && sources+=("$TB_CC_HOOK_SOURCE")
      ;;
    opencode)
      [[ -f "$TRACE_PLUGIN_OPENCODE_PLUGIN_SOURCE" ]] \
        && sources+=("$TRACE_PLUGIN_OPENCODE_PLUGIN_SOURCE")
      [[ -f "$TRACE_PLUGIN_OPENCODE_HOOK_SOURCE" ]] \
        && sources+=("$TRACE_PLUGIN_OPENCODE_HOOK_SOURCE")
      ;;
  esac

  echo "prewarming immutable OpenSandbox S3 objects..."
  python3 "$SCRIPT_DIR/opensandbox_s3_upload.py" preflight
  python3 "$SCRIPT_DIR/opensandbox_s3_upload.py" prewarm "${sources[@]}"
}

harbor_prepare_or_select_wheels() {
  mkdir -p "$RUNTIME_DIR"
  local status_file="${RUNTIME_DIR}/local-deps-prepare.status"
  rm -f "$WORKERS_READY_FILE" "$WORKERS_FAILED_FILE" "$EFFECTIVE_WHEEL_URL_FILE" "$EFFECTIVE_CLAUDE_TGZ_URL_FILE" "$HARBOR_RUNNER_PREPARE_STATUS_FILE"
  : > "$LOCAL_DEPS_LOG_FILE"
  echo "checking" > "$status_file"

  if harbor_local_cache_ready; then
    echo "using local wheel cache"
    harbor_ensure_local_wheels_server
    harbor_write_effective_wheel_source "$TB_LOCAL_WHEEL_SERVER_URL"
    harbor_prewarm_s3_upload_cache || {
      echo "failed" > "$status_file"
      touch "$WORKERS_FAILED_FILE"
      return 1
    }
    echo "done" > "$status_file"
    harbor_mark_workers_ready
    return $?
  fi

  local remote_url
  remote_url="$(harbor_pick_remote_wheel_url || true)"
  if [[ -n "${remote_url:-}" ]]; then
    if [[ "$TB_ENVIRONMENT_TYPE" == "opensandbox" \
      && "$YICLOUD_SANDBOX_UPLOAD_BACKEND" == "s3" ]]; then
      echo "S3 upload cannot use a remote-only dependency cache" >&2
      echo "failed" > "$status_file"
      touch "$WORKERS_FAILED_FILE"
      return 1
    fi
    echo "using remote wheel cache: $remote_url"
    harbor_write_effective_wheel_source "$remote_url"
    echo "remote" > "$status_file"
    harbor_mark_workers_ready
    return $?
  fi

  echo "preparing" > "$status_file"
  echo "local cache missing; downloading dependency cache..."
  local prepare_opencode_cache=0
  if harbor_agent_is_opencode; then
    prepare_opencode_cache=1
  fi
  if (cd "$SCRIPT_DIR" && WHEEL_DIR="$LOCAL_WHEEL_DIR" CACHE_SCHEMA=3 CLAUDE_CODE_VERSION="$CLAUDE_CODE_VERSION" CLAUDE_CODE_TGZ_BASENAME="$CLAUDE_CODE_TGZ_BASENAME" PREPARE_OPENCODE_CACHE="$prepare_opencode_cache" OPENCODE_VERSION="$OPENCODE_VERSION" OPENCODE_TGZ_BASENAME="$OPENCODE_TGZ_BASENAME" OPENCODE_LINUX_X64_TGZ_BASENAME="$OPENCODE_LINUX_X64_TGZ_BASENAME" ./prepare_local_deps.sh 2>&1 | tee -a "$LOCAL_DEPS_LOG_FILE"); then
    harbor_ensure_local_wheels_server
    harbor_write_effective_wheel_source "$TB_LOCAL_WHEEL_SERVER_URL"
    harbor_prewarm_s3_upload_cache || {
      echo "failed" > "$status_file"
      touch "$WORKERS_FAILED_FILE"
      return 1
    }
    echo "done" > "$status_file"
    harbor_mark_workers_ready
    return $?
  fi

  echo "failed" > "$status_file"
  touch "$WORKERS_FAILED_FILE"
  return 1
}

harbor_prepare_agent_runtime() {
  if harbor_agent_is_oracle \
    || [[ "$ROLLOUT" == "1" && "$RL_AGENT" == "oracle" ]] \
    || [[ "$TB_ENVIRONMENT_TYPE" == "e2b" ]]; then
    mkdir -p "$RUNTIME_DIR"
    rm -f "$WORKERS_FAILED_FILE" "$HARBOR_RUNNER_PREPARE_STATUS_FILE"
    if harbor_validate_runner_cli; then
      touch "$WORKERS_READY_FILE"
      return 0
    fi
    touch "$WORKERS_FAILED_FILE"
    return 1
  fi

  if harbor_agent_is_opencode; then
    if ! harbor_prepare_or_select_wheels; then
      echo "failed to prepare local dependency cache" >&2
      touch "$WORKERS_FAILED_FILE"
      return 1
    fi
    return 0
  fi

  if ! harbor_prepare_or_select_wheels; then
    echo "failed to prepare local dependency cache" >&2
    touch "$WORKERS_FAILED_FILE"
    return 1
  fi
}

harbor_validate_runner_cli() {
  python3 "$SCRIPT_DIR/harbor_prepare_runner_cli.py"
}

harbor_runner_cli_ready() {
  if [[ "$HARBOR_RUNNER_PREPARE" != "1" ]]; then
    [[ -x "$HARBOR_OPIK_BIN" && -x "$HARBOR_CLI_BIN" ]]
    return
  fi
  [[ -x "$HARBOR_OPIK_BIN" ]] \
    && [[ -x "$HARBOR_CLI_BIN" ]] \
    && [[ -x "$HARBOR_OPIK_PYTHON" ]] \
    && [[ "$(cat "$HARBOR_RUNNER_PREPARE_STATUS_FILE" 2>/dev/null || true)" == "done" ]]
}

harbor_mark_workers_ready() {
  if harbor_validate_runner_cli; then
    touch "$WORKERS_READY_FILE"
    return 0
  fi
  return 1
}
