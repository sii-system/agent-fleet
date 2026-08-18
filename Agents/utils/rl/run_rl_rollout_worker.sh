#!/usr/bin/env bash
set -euo pipefail

RL_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARBOR_SCRIPT_DIR="${HARBOR_SCRIPT_DIR:-$(cd "$RL_SCRIPT_DIR/../common/Harbor" && pwd)}"
. "$HARBOR_SCRIPT_DIR/env.sh"

WORKER_ID="${1:?worker id required}"
PENDING_DIR="$RL_QUEUE_DIR/pending"
ACTIVE_QUEUE_DIR="$RL_QUEUE_DIR/active"
RESULTS_DIR="$RL_QUEUE_DIR/results"
WORKLIST_DIR="$RL_QUEUE_DIR/worklists"
WORKER_LOG="$RUNTIME_DIR/rl-worker-${WORKER_ID}.log"
CURRENT_FILE="$ACTIVE_QUEUE_DIR/worker-${WORKER_ID}.current"
AGENT_TAIL_PID=""

mkdir -p "$PENDING_DIR" "$ACTIVE_QUEUE_DIR" "$RESULTS_DIR" "$WORKLIST_DIR" "$RUNTIME_DIR/worker-logs"

log_msg() {
  printf '[%s] [rl-worker-%s] %s\n' "$(date '+%F %T')" "$WORKER_ID" "$1" | tee -a "$WORKER_LOG"
}

safe_name() {
  printf '%s' "$1" | tr '/[:space:]' '___' | tr -cd 'A-Za-z0-9._-'
}

clear_pane_history() {
  # Logs remain on disk; each pane should show only its current rollout task.
  printf '\033[3J\033[H\033[2J'
}

rename_pane() {
  local name="$1"
  # Pane naming is cosmetic and must never stop a rollout worker.  zellij can
  # reject the action when the pane has no attached client during startup.
  if [[ -n "${ZELLIJ_SESSION_NAME:-}" ]] && command -v zellij >/dev/null 2>&1; then
    zellij action rename-pane "$name" >/dev/null 2>&1 || true
  fi
}

json_get() {
  python3 - "$1" "$2" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
value = data
for part in sys.argv[2].split("."):
    if not part:
        continue
    if not isinstance(value, dict):
        value = ""
        break
    value = value.get(part, "")
print("" if value is None else value)
PY
}

json_get_first() {
  python3 - "$@" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))

def get_path(obj, path):
    value = obj
    for part in path.split("."):
        if not part:
            continue
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value

def format_value(value):
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)

for path in sys.argv[2:]:
    formatted = format_value(get_path(data, path))
    if formatted:
        print(formatted)
        break
PY
}

json_build_result() {
  python3 - "$@" <<'PY'
import json
import os
import sys
from pathlib import Path

(
    request_file,
    result_file,
    console_log,
    reward,
    exception_type,
    exit_code,
    result_out,
    status,
) = sys.argv[1:9]
request = json.loads(Path(request_file).read_text(encoding="utf-8"))
result_data = {}
if result_file:
    try:
        result_data = json.loads(Path(result_file).read_text(encoding="utf-8"))
    except Exception:
        result_data = {}
agent_result = result_data.get("agent_result") if isinstance(result_data, dict) else None
verifier_result = result_data.get("verifier_result") if isinstance(result_data, dict) else None
exception_info = result_data.get("exception_info") if isinstance(result_data, dict) else None
if not isinstance(exception_info, dict) and exception_type:
    exception_info = {"exception_type": exception_type}
payload = {
    "ok": status == "completed" and not exception_type,
    "task_id": request.get("task_id"),
    "task_path": request.get("task_path"),
    "ray_submission_id": request.get("ray_submission_id"),
    "polar_task_id": request.get("polar_task_id"),
    "display_name": request.get("display_name"),
    "environment_type": request.get("environment_type"),
    "trial_name": Path(result_file).parent.name if result_file else "",
    "trial_uri": str(Path(result_file).parent) if result_file else "",
    "reward": float(reward) if str(reward).strip() not in {"", "None"} else None,
    "rollout_details": agent_result.get("rollout_details") if isinstance(agent_result, dict) else None,
    "num_turns": (agent_result.get("metadata") or {}).get("n_episodes") if isinstance(agent_result, dict) else None,
    "agent_result": agent_result,
    "verifier_result": verifier_result,
    "exception_info": exception_info,
    "metadata": {
        "request_id": request.get("request_id"),
        "session_id": request.get("session_id"),
        "ray_submission_id": request.get("ray_submission_id"),
        "polar_task_id": request.get("polar_task_id"),
        "display_name": request.get("display_name"),
        "console_log": console_log,
        "exit_code": int(exit_code),
    },
}
result_path = Path(result_out)
tmp_path = result_path.with_name(f".{result_path.name}.{os.getpid()}.tmp")
tmp_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")
tmp_path.replace(result_path)
PY
}

find_latest_trial_result() {
  python3 "$HARBOR_SCRIPT_DIR/harbor_worker_utils.py" latest-result "$1"
}

summarize_result() {
  python3 "$HARBOR_SCRIPT_DIR/harbor_worker_utils.py" summarize-result "$1"
}

start_agent_log_stream() {
  local target="$1"
  if [[ "$RL_AGENT" == "opencode" ]]; then
    setsid python3 "$HARBOR_SCRIPT_DIR/harbor_worker_utils.py" stream-opencode-log "$target" &
  elif [[ "$RL_AGENT" == "claude-code" ]]; then
    setsid python3 "$HARBOR_SCRIPT_DIR/harbor_worker_utils.py" stream-claude-log "$target" &
  else
    return 0
  fi
  AGENT_TAIL_PID="$!"
}

stop_agent_log_stream() {
  if [[ -n "${AGENT_TAIL_PID:-}" ]]; then
    # The streamer runs in its own process group so stopping it also stops any
    # child tailer it spawned; otherwise one orphan process is left per task.
    kill -- "-$AGENT_TAIL_PID" >/dev/null 2>&1 || kill "$AGENT_TAIL_PID" >/dev/null 2>&1 || true
    wait "$AGENT_TAIL_PID" >/dev/null 2>&1 || true
    AGENT_TAIL_PID=""
  fi
}

find_trial_logs_dir() {
  local result_file="$1"
  local result_dir
  result_dir="$(dirname "$result_file")"
  if [[ -d "$result_dir/logs" ]]; then
    printf '%s\n' "$result_dir/logs"
    return 0
  fi
  if [[ -f "$result_dir/agent/opencode.txt" ]]; then
    printf '%s\n' "$result_dir/agent"
    return 0
  fi
  if [[ -d "$result_dir/agent/sessions" ]]; then
    printf '%s\n' "$result_dir"
    return 0
  fi
  find "$result_dir" -mindepth 1 -maxdepth 1 -type d -print | head -n 1
}

finalize_timeout_trace() {
  local result_file="$1"
  # Rollout workers have their own timeout finalization path. Keep it behind
  # the same trace switch as the fixed-dataset worker so trace-off runs never
  # replay Claude or OpenCode hook backups into Opik.
  if ! harbor_trace_to_opik_enabled; then
    log_msg "timeout finalize skipped: TRACE_TO_OPIK=false"
    return 0
  fi
  local project_name="${2:-${OPIK_PROJECT_NAME:-}}"
  local run_id="${3:-${HARBOR_RUN_ID:-}}"
  local task_id="${4:-${HARBOR_TASK_ID:-}}"
  local logs_dir py normalized_opik_url
  logs_dir="$(find_trial_logs_dir "$result_file" || true)"
  [[ -n "${logs_dir:-}" && -d "$logs_dir" ]] || return 0
  py="${HARBOR_OPIK_PYTHON:-/opt/harbor-runner/bin/python}"
  [[ -x "$py" ]] || py="python3"
  normalized_opik_url="${OPIK_URL_OVERRIDE:-${OPIK_URL:-}}"
  normalized_opik_url="${normalized_opik_url%/}"
  if [[ -n "$normalized_opik_url" && "$normalized_opik_url" != */api ]]; then
    normalized_opik_url="${normalized_opik_url}/api"
  fi
  if [[ -n "$normalized_opik_url" ]]; then
    export OPIK_URL_OVERRIDE="$normalized_opik_url"
    export OPIK_URL="$normalized_opik_url"
  fi
  export OPIK_PROJECT_NAME="$project_name"
  export HARBOR_RUN_ID="$run_id"
  export HARBOR_TASK_ID="$task_id"
  if [[ "$RL_AGENT" == "opencode" ]]; then
    "$py" "$HARBOR_OPENCODE_DIR/finalize_opencode_sessions.py" \
      --status timeout --logs-dir "$logs_dir" >> "$WORKER_LOG" 2>&1 || true
  elif [[ "$RL_AGENT" == "claude-code" ]]; then
    python3 "$HARBOR_SCRIPT_DIR/harbor_worker_utils.py" prepare-claude-timeout-backup \
      "$logs_dir" --project-name "$OPIK_PROJECT_NAME" >> "$WORKER_LOG" 2>&1 || true
    "$py" "$TRACE_PLUGIN_CLAUDE_HOOK_SOURCE" \
      ReplayTimeout --logs-dir "$logs_dir" >> "$WORKER_LOG" 2>&1 || true
  fi
}

claim_request() {
  local pending active
  shopt -s nullglob
  for pending in "$PENDING_DIR"/*.json; do
    active="$ACTIVE_QUEUE_DIR/$(basename "$pending")"
    if mv "$pending" "$active" 2>/dev/null; then
      printf '%s\n' "$active"
      return 0
    fi
  done
  return 1
}

cleanup() {
  rm -f "$CURRENT_FILE"
  stop_agent_log_stream
}
trap cleanup EXIT

while true; do
  request_file="$(claim_request || true)"
  if [[ -z "${request_file:-}" ]]; then
    sleep 1
    continue
  fi

  request_id="$(json_get "$request_file" request_id)"
  request_file_id="$(json_get "$request_file" request_file_id)"
  request_file_id="${request_file_id:-$request_id}"
  task_name="$(json_get "$request_file" task_id)"
  dataset_root="$(json_get "$request_file" dataset_root)"
  model_name="$(json_get "$request_file" model_name)"
  api_base="$(json_get_first "$request_file" api_base trial_config.agent.kwargs.api_base)"
  api_key="${RL_API_KEY:-${API_KEY:-}}"
  session_id="$(json_get "$request_file" session_id)"
  ray_submission_id="$(json_get "$request_file" ray_submission_id)"
  opik_project_name="$(json_get "$request_file" opik_project_name)"
  polar_task_id="$(json_get "$request_file" polar_task_id)"
  display_name="$(json_get "$request_file" display_name)"
  force_build="$(json_get_first "$request_file" force_build trial_config.environment.force_build)"
  environment_type="$(json_get_first "$request_file" environment_type trial_config.environment.type)"
  max_new_tokens="$(json_get_first "$request_file" max_new_tokens trial_config.agent.kwargs.max_new_tokens)"
  model_info="$(json_get_first "$request_file" model_info trial_config.agent.kwargs.model_info)"
  claude_max_output_tokens="$(json_get_first "$request_file" claude_code_max_output_tokens trial_config.agent.kwargs.claude_code_max_output_tokens)"
  max_turns="$(json_get_first "$request_file" max_turns trial_config.agent.kwargs.max_turns)"
  temperature="$(json_get_first "$request_file" temperature trial_config.agent.kwargs.temperature trial_config.agent.kwargs.llm_kwargs.temperature)"
  top_p="$(json_get_first "$request_file" top_p trial_config.agent.kwargs.llm_kwargs.top_p trial_config.agent.kwargs.top_p)"
  top_k="$(json_get_first "$request_file" top_k trial_config.agent.kwargs.llm_kwargs.top_k trial_config.agent.kwargs.top_k)"
  min_p="$(json_get_first "$request_file" min_p trial_config.agent.kwargs.llm_kwargs.min_p trial_config.agent.kwargs.min_p)"
  llm_timeout="$(json_get_first "$request_file" llm_timeout trial_config.agent.kwargs.llm_kwargs.timeout)"
  llm_max_retries="$(json_get_first "$request_file" llm_max_retries trial_config.agent.kwargs.llm_kwargs.max_retries)"
  agent_timeout_multiplier="$(json_get_first "$request_file" agent_timeout_multiplier trial_config.agent.agent_timeout_multiplier)"
  collect_rollout_details="$(json_get_first "$request_file" collect_rollout_details trial_config.agent.kwargs.collect_rollout_details)"
  enable_summarize="$(json_get_first "$request_file" enable_summarize trial_config.agent.kwargs.enable_summarize)"
  if [[ -z "$display_name" ]]; then
    suffix="${polar_task_id:-$session_id}"
    suffix="${suffix: -6}"
    display_name="${task_name}${suffix:+-$suffix}"
  fi
  task_safe="$(safe_name "$display_name")"
  task_jobs_root="$JOBS_ROOT/rl-worker-${WORKER_ID}/${request_id}-${task_safe}"
  task_console_log="$task_jobs_root/console.log"
  result_out="$RESULTS_DIR/${request_file_id}.json"

  mkdir -p "$task_jobs_root"
  printf '%s\t%s\t%s\t%s\t%s\n' "$request_id" "$task_name" "$display_name" "$ray_submission_id" "$polar_task_id" > "$CURRENT_FILE"
  clear_pane_history
  rename_pane "$display_name"
  environment_type="${environment_type:-${RL_ENVIRONMENT_TYPE:-docker}}"
  log_msg "starting request=${request_id} display=${display_name} task=${task_name} environment=${environment_type} ray_submission=${ray_submission_id:-none} polar_task=${polar_task_id:-none}"

  if [[ -n "$dataset_root" ]]; then
    worklist="$WORKLIST_DIR/$(safe_name "$dataset_root").txt"
    if [[ ! -s "$worklist" ]]; then
      python3 "$RL_SCRIPT_DIR/rl_dataset_worklist.py" "$dataset_root" \
        --output "$worklist" --disabled-task-ids "$RL_DISABLED_TASK_IDS" >> "$WORKER_LOG" 2>&1 || true
    fi
  fi

  if [[ "${HARBOR_DRY_RUN:-0}" != "1" ]]; then
    start_agent_log_stream "$task_jobs_root"
  fi

  set +e
  (
    export DATASET_PATH="$dataset_root"
    export AGENT="$RL_AGENT"
    export MODEL="$model_name"
    export HARBOR_MODEL="$model_name"
    export RL_MODEL_NAME="$model_name"
    export HARBOR_RUN_ID="$ray_submission_id"
    export OPIK_PROJECT_NAME="$opik_project_name"
    if ! request_headers_json="$(
      MODEL_REQUEST_SESSION_ID="$session_id" \
        python3 "$RL_SCRIPT_DIR/rollout_worker_utils.py" request-headers
    )"; then
      echo "[ERROR] invalid MODEL_REQUEST_CONFIG_JSON" >&2
      exit 1
    fi
    if [[ "$RL_AGENT" == "claude-code" ]]; then
      # env.sh derives these aliases when the listener starts. Override every
      # alias per request so a later rollout model cannot inherit that default.
      export HARBOR_ANTHROPIC_MODEL="$model_name"
      export HARBOR_ANTHROPIC_DEFAULT_OPUS_MODEL="$model_name"
      export HARBOR_ANTHROPIC_DEFAULT_SONNET_MODEL="$model_name"
      export HARBOR_ANTHROPIC_DEFAULT_HAIKU_MODEL="$model_name"
      export HARBOR_CLAUDE_CODE_SUBAGENT_MODEL="$model_name"
    elif [[ "$RL_AGENT" == "opencode" ]]; then
      # Force env.sh to rebuild the custom-provider JSON from this request's
      # model, endpoint, and key instead of reusing the listener-time snapshot.
      export OPENCODE_CONFIG_CONTENT=""
    fi
    export HARBOR_ENVIRONMENT_TYPE="$environment_type"
    if [[ "$environment_type" == "e2b" && -n "${HARBOR_E2B_PREBUILT_TEMPLATE:-}" ]]; then
      export HARBOR_ENVIRONMENT_SPEC="$HARBOR_E2B_PREBUILT_ENVIRONMENT_SPEC"
    elif [[ "$environment_type" == "opensandbox" ]]; then
      export HARBOR_ENVIRONMENT_SPEC="yicloud_opensandbox:YiCloudOpenSandboxEnvironment"
    elif [[ "$environment_type" == "qz" ]]; then
      export HARBOR_ENVIRONMENT_SPEC="qz_e2b_sandbox:QzSandboxEnvironment"
    elif [[ "$environment_type" != "${RL_ENVIRONMENT_TYPE:-docker}" ]]; then
      # A per-request backend override must not inherit the listener's default
      # environment import path.
      export HARBOR_ENVIRONMENT_SPEC="$environment_type"
    fi
    export HARBOR_E2B_SANDBOX_TIMEOUT_SEC="${RL_E2B_SANDBOX_TIMEOUT_SEC:-${HARBOR_E2B_SANDBOX_TIMEOUT_SEC:-}}"
    # Rollout may target Polar gateways with smaller context windows than the
    # normal benchmark defaults. Apply RL_* budgets to the Harbor/Claude args.
    export HARBOR_MAX_NEW_TOKENS="${max_new_tokens:-${RL_MAX_NEW_TOKENS:-${HARBOR_MAX_NEW_TOKENS:-}}}"
    export HARBOR_MODEL_INFO="${model_info:-${RL_MODEL_INFO:-${HARBOR_MODEL_INFO:-}}}"
    export HARBOR_CLAUDE_CODE_MAX_OUTPUT_TOKENS="${claude_max_output_tokens:-${RL_CLAUDE_CODE_MAX_OUTPUT_TOKENS:-${HARBOR_CLAUDE_CODE_MAX_OUTPUT_TOKENS:-}}}"
    export HARBOR_AK_MAX_TURNS="${max_turns:-${RL_MAX_TURNS:-${HARBOR_AK_MAX_TURNS:-}}}"
    export HARBOR_AGENT_TIMEOUT_MULTIPLIER="${agent_timeout_multiplier:-${RL_AGENT_TIMEOUT_MULTIPLIER:-${HARBOR_AGENT_TIMEOUT_MULTIPLIER:-}}}"
    if [[ "$RL_AGENT" == "claude-code" ]]; then
      export HARBOR_AK_COLLECT_ROLLOUT_DETAILS="${collect_rollout_details:-${RL_COLLECT_ROLLOUT_DETAILS:-${HARBOR_AK_COLLECT_ROLLOUT_DETAILS:-}}}"
      export HARBOR_AK_ENABLE_SUMMARIZE="${enable_summarize:-${RL_ENABLE_SUMMARIZE:-${HARBOR_AK_ENABLE_SUMMARIZE:-}}}"
    fi
    if [[ "$request_headers_json" != "{}" ]]; then
      # Claude Code does not read Harbor's llm_kwargs.extra_headers. Apply the
      # same effective request headers through its supported client setting.
      export HARBOR_ANTHROPIC_CUSTOM_HEADERS="$(
        MODEL_REQUEST_HEADERS_JSON="$request_headers_json" \
        HARBOR_ANTHROPIC_CUSTOM_HEADERS="${HARBOR_ANTHROPIC_CUSTOM_HEADERS:-}" \
          python3 "$RL_SCRIPT_DIR/rollout_worker_utils.py" render-header-lines
      )"
    fi
    if [[ -n "$api_base" ]]; then
      api_root="${api_base%/}"
      api_root="${api_root%/chat/completions}"
      api_root="${api_root%/v1}"
      export BASE_URL="$api_root"
      export HARBOR_API_BASE="${api_root}/v1/chat/completions"
      # The inherited HARBOR_ANTHROPIC_BASE_URL is initialized from the listener's
      # default BASE_URL. A request-specific api_base must replace that stale
      # value unless the host explicitly configured a separate Anthropic API.
      export HARBOR_ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-$api_root}"
    fi
    if [[ -n "$api_key" ]]; then
      export API_KEY="$api_key"
      export HARBOR_ANTHROPIC_AUTH_TOKEN="$api_key"
    fi
    export HARBOR_LLM_KWARGS="$(
      MODEL_REQUEST_HEADERS_JSON="$request_headers_json" \
        python3 - "${temperature:-${RL_TEMPERATURE:-}}" \
        "${top_p:-${RL_TOP_P:-}}" \
        "${top_k:-${RL_TOP_K:-}}" \
        "${min_p:-${RL_MIN_P:-}}" \
        "${llm_timeout:-${RL_LLM_TIMEOUT:-}}" \
        "${llm_max_retries:-${RL_LLM_MAX_RETRIES:-}}" <<'PY'
import json
import os
import sys

temperature, top_p, top_k, min_p, timeout, max_retries = sys.argv[1:7]
payload = {}

def add_number(name, value, cast=float):
    value = str(value).strip()
    if value == "":
        return
    try:
        payload[name] = cast(value)
    except ValueError:
        payload[name] = value

add_number("temperature", temperature)
add_number("top_p", top_p)
add_number("top_k", top_k, int)
add_number("min_p", min_p)
add_number("timeout", timeout)
add_number("max_retries", max_retries, int)
headers = json.loads(os.environ["MODEL_REQUEST_HEADERS_JSON"])
if headers:
    payload["extra_headers"] = headers
print(json.dumps(payload, separators=(",", ":")))
PY
    )"
    export HARBOR_TASK_ID="$task_name"
    export HARBOR_INCLUDE_TASKS="$task_name"
    export INCLUDE_TASKS="$task_name"
    export HARBOR_LIMIT=""
    export HARBOR_RUNS=1
    export HARBOR_N_CONCURRENT=1
    export JOBS_ROOT="$task_jobs_root"
    case "${force_build:-${RL_FORCE_BUILD:-${HARBOR_FORCE_BUILD:-0}}}" in
      1|true|TRUE|True|yes|YES|Yes|on|ON|On) export HARBOR_FORCE_BUILD=1 ;;
      *) export HARBOR_FORCE_BUILD=0 ;;
    esac
    bash "$HARBOR_SCRIPT_DIR/harboropik.sh"
  ) 2>&1 | tee "$task_console_log"
  rc=${PIPESTATUS[0]}
  set -e

  stop_agent_log_stream

  result_file="$(find_latest_trial_result "$task_jobs_root" || true)"
  reward=""
  exception_type=""
  status="failed"
  if [[ -n "${result_file:-}" ]] && summary="$(summarize_result "$result_file")"; then
    reward="$(echo "$summary" | sed -n '1p')"
    exception_type="$(echo "$summary" | sed -n '2p')"
    if [[ "${exception_type:-}" == "AgentTimeoutError" ]]; then
      finalize_timeout_trace "$result_file" "$opik_project_name" "$ray_submission_id" "$task_name"
    fi
    if [[ -z "${exception_type:-}" && "$rc" -eq 0 ]]; then
      status="completed"
    fi
  fi

  json_build_result "$request_file" "${result_file:-}" "$task_console_log" "$reward" "$exception_type" "$rc" "$result_out" "$status"
  printf '{"event":"finish","timestamp":"%s","request_id":"%s","task_id":"%s","display_name":"%s","environment_type":"%s","ray_submission_id":"%s","polar_task_id":"%s","status":"%s","reward":"%s","exception_type":"%s"}\n' \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$request_id" "$task_name" "$display_name" "$environment_type" "$ray_submission_id" "$polar_task_id" "$status" "$reward" "$exception_type" >> "$RL_TRACE_LOG"
  log_msg "finished request=${request_id} display=${display_name} task=${task_name} status=${status} reward=${reward:-none} exception=${exception_type:-none} rc=${rc}"
  rm -f "$CURRENT_FILE" "$request_file"
  if ! python3 "$RL_SCRIPT_DIR/rollout_worker_utils.py" prune-trials \
    "$JOBS_ROOT/rl-worker-${WORKER_ID}" \
    --keep "${RL_KEEP_TRIALS_PER_WORKER:-20}"; then
    log_msg "warning: failed to prune old rollout artifacts"
  fi
done
