#!/usr/bin/env bash
# Optional rollout mode configuration for Miles/Polar remote Harbor rollout.
# Source this file only via ROLLOUT=1 ./start.sh.  Keep all new variables under
# RL_* so the normal benchmark TUI path remains controlled by env.sh.

RL_HOST="${RL_HOST:-0.0.0.0}"
RL_PORT="${RL_PORT:-19001}"

# Dataset roots exposed by the remote Harbor service.  RL_DATASET_ROOTS accepts
# comma-separated name=path pairs, for example:
#   seta=/workspace/seta-env/Harbor-Dataset,smith=/workspace/harbor/datasets/swesmith
RL_DATASET_NAME="${RL_DATASET_NAME:-${DATASET_NAME:-seta}}"
RL_DATASET_ROOT="${RL_DATASET_ROOT:-${DATASET_PATH:-/workspace/seta-env/Harbor-Dataset}}"
RL_DATASET_ROOTS="${RL_DATASET_ROOTS:-}"
RL_DISABLED_TASK_IDS="${RL_DISABLED_TASK_IDS:-}"
# Optional trusted benchmark result processor. The listener host chooses this
# path; /run_trial requests can never supply or override it.
RL_BENCHMARK="${RL_BENCHMARK:-}"
RL_RESULT_PROCESSOR="${RL_RESULT_PROCESSOR:-}"

RL_TRIALS_DIR="${RL_TRIALS_DIR:-${OUTPUT_ROOT:-/workspace/runs}/rl-remote-trials}"
RL_MAX_CONCURRENT="${RL_MAX_CONCURRENT:-16}"

# Default agent is claude-code for this repo.  Set RL_AGENT=terminus-2 to compare
# with the current Miles/Polar remote Harbor runner behavior.
RL_AGENT="${RL_AGENT:-claude-code}"
RL_MODEL_NAME="${RL_MODEL_NAME:-${MODEL:-minimax2.7}}"
RL_MODEL_PREFIX="${RL_MODEL_PREFIX:-hosted_vllm}"
if [[ -z "${RL_API_BASE:-}" && -n "${BASE_URL:-}" ]]; then
  RL_API_BASE="${BASE_URL%/}/v1"
fi
RL_API_BASE="${RL_API_BASE:-}"
RL_API_KEY="${RL_API_KEY:-${API_KEY:-}}"
RL_API_KEY_MODE="${RL_API_KEY_MODE:-static}"
# Trusted, host-side request customization. The version-1 contract supports
# headers.set and is never accepted from /run_trial request payloads.
MODEL_REQUEST_CONFIG_JSON="${MODEL_REQUEST_CONFIG_JSON:-}"

RL_ENVIRONMENT_TYPE="${RL_ENVIRONMENT_TYPE:-docker}"
RL_E2B_SANDBOX_TIMEOUT_SEC="${RL_E2B_SANDBOX_TIMEOUT_SEC:-}"
# Host-only compatibility setting. The template ID is inherited by workers and
# must never be accepted from /run_trial request payloads.
RL_E2B_PREBUILT_TEMPLATE="${RL_E2B_PREBUILT_TEMPLATE:-${E2B_TEMPLATE:-}}"
RL_FORCE_BUILD="${RL_FORCE_BUILD:-${HARBOR_FORCE_BUILD:-0}}"
if [[ -z "${RL_MODEL_INFO:-}" ]]; then
  # Polar rollout currently serves 32k-context models by default. Keep the
  # advertised input + output budget within that limit so Claude does not
  # request an oversized completion before the rollout trace is created.
  RL_MODEL_INFO='{"max_input_tokens":24576,"max_output_tokens":8192}'
fi
RL_MAX_NEW_TOKENS="${RL_MAX_NEW_TOKENS:-8192}"
RL_CLAUDE_CODE_MAX_OUTPUT_TOKENS="${RL_CLAUDE_CODE_MAX_OUTPUT_TOKENS:-$RL_MAX_NEW_TOKENS}"
RL_MAX_TURNS="${RL_MAX_TURNS:-32}"
# Harbor exposes agent timeout as a multiplier, not an absolute second value.
RL_AGENT_TIMEOUT_MULTIPLIER="${RL_AGENT_TIMEOUT_MULTIPLIER:-${HARBOR_AGENT_TIMEOUT_MULTIPLIER:-}}"
RL_LLM_TIMEOUT="${RL_LLM_TIMEOUT:-900}"
RL_LLM_MAX_RETRIES="${RL_LLM_MAX_RETRIES:-0}"
RL_TEMPERATURE="${RL_TEMPERATURE:-1.0}"
RL_TOP_P="${RL_TOP_P:-1.0}"
RL_TOP_K="${RL_TOP_K:--1}"
RL_MIN_P="${RL_MIN_P:-0.0}"
RL_COLLECT_ROLLOUT_DETAILS="${RL_COLLECT_ROLLOUT_DETAILS:-true}"
RL_ENABLE_SUMMARIZE="${RL_ENABLE_SUMMARIZE:-false}"

RL_TRACE_LOG="${RL_TRACE_LOG:-${RUNTIME_DIR:-/workspace/runs/rl-rollout}/rl-rollout-requests.jsonl}"
RL_SERVER_LOG="${RL_SERVER_LOG:-${RUNTIME_DIR:-/workspace/runs/rl-rollout}/rl-rollout-server.log}"
RL_SERVER_PID_FILE="${RL_SERVER_PID_FILE:-${RUNTIME_DIR:-/workspace/runs/rl-rollout}/rl-rollout-server.pid}"
RL_QUEUE_DIR="${RL_QUEUE_DIR:-${RUNTIME_DIR:-/workspace/runs/rl-rollout}/rl-queue}"
RL_ACTIVE_DIR="${RL_ACTIVE_DIR:-${RL_QUEUE_DIR}/active}"
RL_JOB_QUEUE_ROOT="${RL_JOB_QUEUE_ROOT:-${RL_QUEUE_DIR}/jobs}"
RL_JOB_RUNTIME_ROOT="${RL_JOB_RUNTIME_ROOT:-${RUNTIME_DIR:-/workspace/runs/rl-rollout}/rl-jobs}"
RL_DYNAMIC_JOB_ZELLIJ="${RL_DYNAMIC_JOB_ZELLIJ:-1}"
RL_WORKERS="${RL_WORKERS:-${RL_MAX_CONCURRENT:-${TOTAL_WORKERS:-16}}}"
RL_REQUEST_TIMEOUT="${RL_REQUEST_TIMEOUT:-3600}"
# Bound rollout-only trial artifacts without changing normal Harbor retention.
RL_KEEP_TRIALS_PER_WORKER="${RL_KEEP_TRIALS_PER_WORKER:-20}"
