#!/usr/bin/env bash
set -euo pipefail

VERIFIER_UV_BIN_DIR_SOURCE=""

cleanup_verifier_uv_bin_dir() {
  [[ -n "$VERIFIER_UV_BIN_DIR_SOURCE" ]] || return 0
  rm -rf -- "$VERIFIER_UV_BIN_DIR_SOURCE"
}

# Each invocation owns one verifier backup; remove it on success or failure.
trap cleanup_verifier_uv_bin_dir EXIT

# harboropik.sh — One-click Terminal Bench runner with real-time Opik tracing
#
# This script orchestrates end-to-end evaluation of agent tasks through
# Harbor (Opik's agent execution framework) and streams every agent lifecycle
# event to an Opik observability project in real time.
#
# Workflow:
#   1. Validate prerequisites (git, curl, python3, Docker daemon)
#   2. Normalize the Opik API URL (ensures /api suffix is present)
#   3. Apply minimal-test defaults when TB_MIN_TEST=1 (fast smoke test)
#   4. Docker Hub connectivity preflight (warn or abort if unreachable)
#   5. Ensure Opik is available:
#        - OPIK_MODE=local  → clone Opik repo and start via docker-compose
#        - OPIK_MODE=remote → verify health and ingestion endpoints
#   6. Clone the Terminal Bench dataset if not already present locally
#   7. Build and execute with the pinned runner's `opik harbor run` command,
#      with PYTHONPATH pointing at Harbor-claude-code so that sitecustomize.py
#      is auto-loaded by Python and patches Harbor's ClaudeCode agent class
#      to enable realtime Opik hooks and fallback trajectory recovery.
#
# All variables have sensible defaults and are fully overridable via the
# environment.  See README.md for the complete variable reference.
#
# Usage examples:
#   TB_MIN_TEST=1 bash harboropik.sh                    # quick smoke test
#   TB_DRY_RUN=1  bash harboropik.sh                    # print command, skip run
#   OPIK_MODE=remote OPIK_BASE=http://host:5173 \
#     TB_RUNS=10 TB_N_CONCURRENT=4 bash harboropik.sh   # standard remote run

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/env.sh"

harbor_is_native_registry_main() {
  [[ "$ROLLOUT" != "1" ]] \
    && harbor_uses_registry_dataset \
    && [[ "${HARBOR_QUEUE_WORKER:-0}" != "1" ]]
}

write_harbor_registry_summary() {
  local exit_code="$1"
  local job_dir=""
  local dataset

  [[ "$TB_DRY_RUN" != "1" ]] || return 0
  [[ -f "$HARBOR_JOB_DIR_FILE" ]] && job_dir="$(cat "$HARBOR_JOB_DIR_FILE" 2>/dev/null || true)"
  dataset="$(harbor_registry_dataset_name)"
  python3 "$SCRIPT_DIR/scripts/write_harbor_registry_summary.py" \
    "$job_dir" "$OUTPUT_PATH/summary.txt" "$exit_code" "$dataset"
}

if harbor_is_native_registry_main; then
  : > "$HARBOR_JOB_DIR_FILE"
  rm -f "$HARBOR_BENCHMARK_EXIT_FILE"
  # BASHPID needs bash >= 4; at this top-level scope $$ is the same pid.
  printf '%s\t%s\n' "${BASHPID:-$$}" "$(awk '{print $22}' "/proc/${BASHPID:-$$}/stat")" > "$HARBOR_BENCHMARK_PID_FILE"
  record_harbor_benchmark_exit() {
    local rc="$?"
    # The exit file is the completion contract with the registry monitor;
    # write it before the best-effort summary and analyzer teardown.
    printf '%s\n' "$rc" > "$HARBOR_BENCHMARK_EXIT_FILE"
    if ! write_harbor_registry_summary "$rc"; then
      echo "[WARN] failed to write registry summary: $OUTPUT_PATH/summary.txt" >&2
    fi
    if ! harbor_stop_online_analysis; then
      echo "[WARN] failed to stop online analyzer for $OUTPUT_PATH" >&2
    fi
  }
  trap record_harbor_benchmark_exit EXIT
fi

online_env_event() {
  if [[ "$HARBOR_ONLINE_ANALYSIS" != "1" ]]; then
    return 0
  fi

  local phase="$1"
  local component="$2"
  local event="$3"
  local severity="$4"
  local fatal="$5"
  local message="$6"
  if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' '[ONLINE_ENV] {"schema":1,"task_id":null,"task_name":"","phase":"preflight","component":"host_prerequisite","event":"command_unavailable","severity":"critical","fatal":true,"scope":"task","message":"python3 is unavailable; structured event details could not be serialized"}'
    return 0
  fi
  python3 - "$phase" "$component" "$event" "$severity" "$fatal" "$message" <<'PY'
import json
import os
import sys

phase, component, event, severity, fatal, message = sys.argv[1:]
print("[ONLINE_ENV] " + json.dumps({
    "schema": 1,
    "task_id": int(os.environ["TB_TASK_INDEX"]) if os.environ.get("TB_TASK_INDEX", "").isdigit() else None,
    "task_name": os.environ.get("TB_TASK_ID", ""),
    "phase": phase,
    "component": component,
    "event": event,
    "severity": severity,
    "fatal": fatal == "true",
    "scope": "task",
    "message": message,
}, separators=(",", ":")))
PY
}

# harbor_trace_to_opik_enabled comes from env.sh so the worker shares it.
configure_trace_disabled_runtime() {
  if harbor_trace_to_opik_enabled; then
    return 0
  fi

  # Both launcher paths still use the Opik tool environment because it also
  # contains Harbor. Disable the SDK itself so the host-side Harbor decorator
  # cannot enqueue writes after the readiness checks have been skipped. Keep
  # connection values available to this shell, but do not let Harbor/Compose
  # inherit them from the parent environment.
  OPIK_TRACK_DISABLE=true
  export OPIK_TRACK_DISABLE
  export -n OPIK_URL OPIK_URL_OVERRIDE OPIK_BASE OPIK_MODE \
    OPIK_PROJECT_NAME OPIK_API_KEY OPIK_WORKSPACE
}

normalize_opik_url_override() {
  local normalized="${OPIK_URL_OVERRIDE%/}"
  if [[ -z "$normalized" ]]; then
    echo "[ERROR] OPIK_URL_OVERRIDE/OPIK_URL is empty; set it to an Opik API URL such as http://host:5173/api" >&2
    echo "[ERROR] or set TRACE_TO_OPIK=false to run the benchmark without Opik tracing" >&2
    exit 1
  fi
  if [[ "$normalized" != */api ]]; then
    OPIK_URL_OVERRIDE="${normalized}/api"
    echo "[WARN] OPIK_URL_OVERRIDE missing /api, auto-normalized to: $OPIK_URL_OVERRIDE"
  else
    OPIK_URL_OVERRIDE="$normalized"
  fi
}

resolve_opik_health_url() {
  local base="$OPIK_BASE"
  local normalized_override="${OPIK_URL_OVERRIDE%/}"
  if [[ "$normalized_override" =~ ^https?://[^/]+ ]]; then
    base="${BASH_REMATCH[0]}"
  fi
  if [[ "$normalized_override" == */api ]]; then
    base="${normalized_override%/api}"
  fi
  echo "${base%/}/health"
}

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    online_env_event "preflight" "host_prerequisite" "command_unavailable" "critical" "true" "missing required command: $1"
    echo "[ERROR] missing command: $1" >&2
    exit 1
  fi
}

cargo_registry_env_suffix() {
  local suffix="$1"
  suffix="${suffix//-/_}"
  suffix="${suffix//./_}"
  printf '%s\n' "$suffix" | tr '[:lower:]' '[:upper:]'
}

append_rust_package_mirror_env() {
  local flag="$1"
  if [[ -n "${RUSTUP_UPDATE_ROOT:-}" ]]; then
    cmd+=( "$flag" "RUSTUP_UPDATE_ROOT=$RUSTUP_UPDATE_ROOT" )
  fi
  if [[ -n "${RUSTUP_DIST_SERVER:-}" ]]; then
    cmd+=( "$flag" "RUSTUP_DIST_SERVER=$RUSTUP_DIST_SERVER" )
  fi
  if [[ -n "${CARGO_REGISTRY_REPLACE_WITH:-}" && -n "${CARGO_REGISTRY_URL:-}" ]]; then
    local registry_suffix
    registry_suffix="$(cargo_registry_env_suffix "$CARGO_REGISTRY_REPLACE_WITH")"
    cmd+=(
      "$flag" "CARGO_REGISTRY_REPLACE_WITH=$CARGO_REGISTRY_REPLACE_WITH"
      "$flag" "CARGO_REGISTRY_URL=$CARGO_REGISTRY_URL"
      "$flag" "CARGO_SOURCE_CRATES_IO_REPLACE_WITH=$CARGO_REGISTRY_REPLACE_WITH"
      "$flag" "CARGO_SOURCE_${registry_suffix}_REGISTRY=$CARGO_REGISTRY_URL"
      "$flag" "CARGO_REGISTRIES_${registry_suffix}_INDEX=$CARGO_REGISTRY_URL"
    )
  fi
}

append_harbor_unprivileged_docker_compose() {
  cmd+=( --extra-docker-compose "$SCRIPT_DIR/overlays/unprivileged-task.yaml" )
}

ensure_trace_plugin_source_if_needed() {
  if [[ "$TB_DRY_RUN" == "1" ]]; then
    return 0
  fi
  local trace_enabled="${TRACE_TO_OPIK:-}"
  local -a required=()
  if [[ "$AGENT" == "opencode" ]]; then
    if [[ "$trace_enabled" == "true" || "$trace_enabled" == "1" ]]; then
      required=("$TRACE_PLUGIN_OPENCODE_PLUGIN_SOURCE" "$TRACE_PLUGIN_OPENCODE_HOOK_SOURCE")
    fi
  elif harbor_trace_to_opik_enabled &&
    [[ "$trace_enabled" == "true" || "$trace_enabled" == "1" || "$TB_CC_OPIK_ENABLE_HOOK" == "1" ]]; then
    # With tracing off the realtime hook is forced off at command
    # construction, so its source is not required either.
    required=("$TRACE_PLUGIN_CLAUDE_HOOK_SOURCE")
  fi

  local path
  # ${arr[@]+...} keeps the expansion valid for an empty array under
  # `set -u` on bash < 4.4 (macOS 3.2); with tracing off nothing is required.
  for path in ${required[@]+"${required[@]}"}; do
    if [[ ! -f "$path" ]]; then
      echo "[ERROR] trace plugin source missing: $path" >&2
      echo "[ERROR] run 'git submodule update --init --recursive' from $REPO_ROOT, or set TRACE_PLUGIN_SOURCE_DIR explicitly." >&2
      exit 1
    fi
  done
}

prepare_verifier_uv_bin() {
  local target_dir="$1"
  local uv_bin uvx_bin
  if [[ -z "$target_dir" ]]; then
    return 1
  fi
  uv_bin="$(command -v uv || true)"
  uvx_bin="$(command -v uvx || true)"
  if [[ -z "$uv_bin" || -z "$uvx_bin" ]]; then
    echo "[WARN] uv/uvx not found on host; verifier will use its normal uv install path" >&2
    return 1
  fi
  if [[ "$(uname -s 2>/dev/null || true)" != "Linux" ]]; then
    echo "[WARN] host uv backup is only enabled on Linux hosts; verifier will use its normal uv install path" >&2
    return 1
  fi
  if command -v file >/dev/null 2>&1; then
    local uv_file uvx_file
    uv_file="$(file -Lb "$uv_bin" 2>/dev/null || true)"
    uvx_file="$(file -Lb "$uvx_bin" 2>/dev/null || true)"
    if [[ "$uv_file" != *ELF* || "$uvx_file" != *ELF* ]]; then
      echo "[WARN] host uv/uvx are not Linux ELF binaries; verifier will use its normal uv install path" >&2
      return 1
    fi
  fi

  mkdir -p "$target_dir"
  cp -f "$uv_bin" "$target_dir/uv"
  cp -f "$uvx_bin" "$target_dir/uvx"
  chmod +x "$target_dir/uv" "$target_dir/uvx"
  # Keep the backup outside $HOME/.local/bin so the verifier's normal uv
  # installer can write there first. PATH puts this directory after common
  # installer locations, so it only wins when the verifier has no uv of its own.
  cat >"$target_dir/env" <<EOF
export PATH="\$HOME/.local/bin:$TB_VERIFIER_UV_BIN_DIR_MOUNT_PATH:\$PATH"
EOF
}

task_is_included() {
  local target="$1"
  local item

  [[ -n "$INCLUDE_TASKS" ]] || return 1

  IFS=',' read -r -a include_arr <<< "$INCLUDE_TASKS"
  for item in "${include_arr[@]}"; do
    item="${item#"${item%%[![:space:]]*}"}"
    item="${item%"${item##*[![:space:]]}"}"
    [[ "$item" == "$target" ]] && return 0
  done

  return 1
}

single_include_task() {
  local include_value="$1"
  local item result count=0
  local -a include_arr

  [[ -n "$include_value" ]] || return 1
  IFS=',' read -r -a include_arr <<< "$include_value"
  for item in "${include_arr[@]}"; do
    item="${item#"${item%%[![:space:]]*}"}"
    item="${item%"${item##*[![:space:]]}"}"
    if [[ -n "$item" ]]; then
      result="$item"
      count=$((count + 1))
    fi
  done

  if [[ "$count" -eq 1 ]]; then
    printf '%s\n' "$result"
    return 0
  fi
  return 1
}

local_image_exists() {
  docker image inspect "$1" >/dev/null 2>&1
}

maybe_report_fix_git_image() {
  local warmed

  if ! task_is_included "fix-git"; then
    return 0
  fi

  if ! local_image_exists "$FIX_GIT_IMAGE_NAME"; then
    return 0
  fi

  warmed="$(docker image inspect --format "{{ index .Config.Labels \"$FIX_GIT_WARM_LABEL\" }}" "$FIX_GIT_IMAGE_NAME" 2>/dev/null || true)"
  if [[ "$warmed" == "true" ]]; then
    echo "[INFO] using warmed local fix-git image: $FIX_GIT_IMAGE_NAME"
  else
    echo "[INFO] using cached local fix-git image: $FIX_GIT_IMAGE_NAME"
  fi
}

ensure_docker_daemon() {
  need_cmd docker
  if ! docker info >/dev/null 2>&1; then
    online_env_event "preflight" "docker" "daemon_unavailable" "critical" "true" "docker info failed; Harbor cannot execute task containers"
    echo "[ERROR] Docker daemon is not running. Harbor requires Docker to execute tasks." >&2
    echo "[ERROR] start Docker Desktop (or another Docker daemon) and retry." >&2
    echo "[ERROR] quick check: docker info" >&2
    exit 1
  fi
}

ensure_opik_repo() {
  if [[ -d "$OPIK_REPO_DIR/.git" ]]; then
    echo "[INFO] using existing Opik repo: $OPIK_REPO_DIR"
    return 0
  fi
  echo "[INFO] cloning Opik repo"
  git clone https://github.com/comet-ml/opik.git "$OPIK_REPO_DIR"
}

ensure_tb_dataset() {
  if [[ -d "$TB_PATH" ]]; then
    echo "[INFO] using existing TB dataset: $TB_PATH"
    return 0
  fi
  echo "[INFO] cloning TB dataset: $TB_DATASET_GIT_URL"
  git clone "$TB_DATASET_GIT_URL" "$TB_PATH"
}

prepare_local_dataset_if_needed() {
  if harbor_uses_registry_dataset; then
    return 0
  fi

  if [[ "$(harbor_dataset_kind)" == "harbor" ]]; then
    ensure_tb_dataset
  else
    harbor_ensure_dataset
  fi
  harbor_prepare_task_file
}

start_opik_local() {
  if [[ ! -d "$COMPOSE_DIR" ]]; then
    echo "[ERROR] compose directory not found: $COMPOSE_DIR" >&2
    exit 1
  fi

  echo "[INFO] starting local Opik stack"
  (
    cd "$COMPOSE_DIR"
    docker compose --profile opik up -d
  )

  echo "[INFO] waiting for Opik /health"
  for _ in $(seq 1 180); do
    if curl -fsS "$OPIK_BASE/health" >/dev/null 2>&1; then
      echo "[INFO] Opik is ready"
      return 0
    fi
    sleep 2
  done

  echo "[ERROR] Opik is not ready after timeout" >&2
  exit 1
}

verify_opik_reachable() {
  local health_url
  health_url="$(resolve_opik_health_url)"
  echo "[INFO] checking Opik endpoint: ${health_url%/health}"
  curl -fsS "$health_url" >/dev/null
}

verify_opik_ingestion_route() {
  local spans_url="${OPIK_URL_OVERRIDE%/}/v1/private/spans/batch"
  local status
  status="$(
    curl -sS -o /dev/null -w "%{http_code}" -X POST \
      -H "Content-Type: application/json" \
      -H "Comet-Workspace: ${OPIK_WORKSPACE}" \
      --data '{"spans":[]}' \
      "$spans_url"
  )"

  case "$status" in
    2*|400|401|403|422)
      ;;
    404|405)
      echo "[ERROR] Opik ingestion endpoint returned $status: $spans_url" >&2
      echo "[ERROR] this usually means the API prefix is wrong (expected .../api)." >&2
      exit 1
      ;;
    *)
      echo "[WARN] Opik ingestion preflight returned HTTP $status for: $spans_url" >&2
      ;;
  esac
}

docker_hub_preflight_check() {
  if [[ "$TB_SKIP_DOCKERHUB_PREFLIGHT" == "1" ]]; then
    echo "[INFO] TB_SKIP_DOCKERHUB_PREFLIGHT=1, skip Docker Hub connectivity preflight"
    return 0
  fi

  if task_is_included "fix-git" && local_image_exists "$FIX_GIT_IMAGE_NAME"; then
    maybe_report_fix_git_image
    echo "[INFO] local fix-git image present, skip Docker Hub connectivity preflight"
    return 0
  fi

  local timeout="$TB_DOCKERHUB_CHECK_TIMEOUT"
  echo "[INFO] checking Docker Hub connectivity (timeout=${timeout}s)"

  local registry_status
  local auth_status
  local preflight_failed=0
  registry_status="$(
    curl --max-time "$timeout" -sS -o /dev/null -w "%{http_code}" https://registry-1.docker.io/v2/ || true
  )"
  if [[ "$registry_status" != "200" && "$registry_status" != "401" ]]; then
    echo "[WARN] cannot reach https://registry-1.docker.io/v2/ within ${timeout}s (status=${registry_status:-000})" >&2
    preflight_failed=1
  fi

  auth_status="$(
    curl --max-time "$timeout" -sS -o /dev/null -w "%{http_code}" \
      "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/alpine:pull" || true
  )"
  if [[ "$auth_status" != "200" ]]; then
    echo "[WARN] cannot reach https://auth.docker.io token service within ${timeout}s (status=${auth_status:-000})" >&2
    preflight_failed=1
  fi

  if [[ "$preflight_failed" == "1" ]]; then
    if [[ "$TB_DOCKERHUB_PREFLIGHT_STRICT" == "1" ]]; then
      online_env_event "preflight" "docker_registry" "connectivity_unavailable" "critical" "true" "Docker Hub preflight failed in strict mode"
      echo "[ERROR] Docker Hub preflight failed in strict mode." >&2
      echo "[ERROR] fix network/proxy/registry mirror first, or set TB_DOCKERHUB_PREFLIGHT_STRICT=0 / TB_SKIP_DOCKERHUB_PREFLIGHT=1." >&2
      exit 1
    fi
    online_env_event "preflight" "docker_registry" "connectivity_degraded" "warning" "false" "Docker Hub preflight failed; continuing because strict mode is disabled"
    echo "[WARN] Docker Hub preflight failed, continuing (strict mode disabled)." >&2
    echo "[WARN] if image pull fails later, set a proxy/registry mirror and retry." >&2
  fi
}

normalize_json_or_fail() {
  local raw="$1"
  python3 - "$raw" <<'PY'
import json
import os
import sys

raw = sys.argv[1]
try:
    obj = json.loads(raw)
except Exception as exc:
    print(f"INVALID_JSON::{exc}", file=sys.stderr)
    sys.exit(1)
print(json.dumps(obj, separators=(",", ":")))
PY
}

apply_min_test_defaults() {
  if [[ "$TB_MIN_TEST" != "1" ]]; then
    return 0
  fi

  if [[ "$TB_RUNS" == "10" ]]; then
    TB_RUNS="1"
  fi
  if [[ -z "$TB_LIMIT" ]]; then
    TB_LIMIT="1"
  fi
  if [[ -z "$INCLUDE_TASKS" && -n "$TB_MIN_TEST_INCLUDE_TASK" ]]; then
    INCLUDE_TASKS="$TB_MIN_TEST_INCLUDE_TASK"
  fi

  echo "[INFO] TB_MIN_TEST=1 enabled (runs=$TB_RUNS, limit=$TB_LIMIT, include_tasks=$INCLUDE_TASKS)"
}

run_tb() {
  local effective_jobs_root="$JOBS_ROOT"
  harbor_apply_effective_wheel_source
  if ! mkdir -p "$effective_jobs_root" 2>/dev/null; then
    effective_jobs_root="$HOME/harbor_jobs"
    mkdir -p "$effective_jobs_root"
    echo "[WARN] unable to use JOBS_ROOT=$JOBS_ROOT, fallback to $effective_jobs_root"
  fi

  local job_name out_dir
  job_name="$(date +%Y-%m-%d__%H-%M-%S)"
  out_dir="$effective_jobs_root/$job_name"
  if harbor_is_native_registry_main; then
    printf '%s\n' "$out_dir" > "$HARBOR_JOB_DIR_FILE"
  fi
  mkdir -p "$out_dir"

  if [[ "$TB_DRY_RUN" == "1" ]]; then
    echo "[INFO] TB_DRY_RUN=1, skip Opik project preflight check"
  elif ! harbor_trace_to_opik_enabled; then
    echo "[INFO] TRACE_TO_OPIK=false, skip Opik project preflight check"
  else
    local _projects_status
    _projects_status="$(
      curl -sS -o /dev/null -w "%{http_code}" \
        -H "Comet-Workspace: ${OPIK_WORKSPACE}" \
        -H "authorization: ${OPIK_API_KEY}" \
        "${OPIK_URL_OVERRIDE%/}/v1/private/projects?page=1&size=1"
    )"
    case "$_projects_status" in
      2*|401|403)
        ;;
      *)
        echo "[ERROR] Opik project preflight returned HTTP $_projects_status" >&2
        echo "[ERROR] endpoint: ${OPIK_URL_OVERRIDE%/}/v1/private/projects" >&2
        exit 1
        ;;
    esac
  fi

  local normalized_llm_kwargs
  if ! normalized_llm_kwargs="$(normalize_json_or_fail "$TB_LLM_KWARGS")"; then
    online_env_event "agent_setup" "agent_configuration" "invalid_llm_kwargs" "critical" "true" "TB_LLM_KWARGS is not valid JSON"
    echo "[ERROR] TB_LLM_KWARGS is not valid JSON" >&2
    echo "[ERROR] current TB_LLM_KWARGS: $TB_LLM_KWARGS" >&2
    exit 1
  fi
  VERIFIER_UV_BIN_DIR_SOURCE="$(mktemp -d "${RUNTIME_DIR%/}/verifier-uv.${job_name}.XXXXXX" 2>/dev/null || true)"
  if [[ -n "$VERIFIER_UV_BIN_DIR_SOURCE" ]]; then
    prepare_verifier_uv_bin "$VERIFIER_UV_BIN_DIR_SOURCE" || true
  else
    echo "[WARN] failed to create verifier uv backup dir; verifier will use its normal uv install path" >&2
  fi

  local effective_tb_task_id include_task
  effective_tb_task_id="${TB_TASK_ID:-}"
  if [[ -z "$effective_tb_task_id" ]]; then
    include_task="$(single_include_task "${INCLUDE_TASKS:-${TB_INCLUDE_TASKS:-}}" || true)"
    if [[ -n "$include_task" ]]; then
      effective_tb_task_id="$include_task"
    fi
  fi

  if [[ -z "$TB_ANTHROPIC_AUTH_TOKEN" ]]; then
    local inferred_api_key
    inferred_api_key="$(
      python3 - "$normalized_llm_kwargs" <<'PY'
import json
import os
import sys

obj = json.loads(sys.argv[1])
api_key = obj.get("api_key", "")
if isinstance(api_key, str):
    print(api_key)
PY
    )"
    if [[ -n "$inferred_api_key" ]]; then
      TB_ANTHROPIC_AUTH_TOKEN="$inferred_api_key"
      echo "[INFO] TB_ANTHROPIC_AUTH_TOKEN is empty; using api_key from TB_LLM_KWARGS"
    else
      online_env_event "agent_setup" "agent_configuration" "auth_token_missing" "critical" "true" "TB_ANTHROPIC_AUTH_TOKEN and TB_LLM_KWARGS.api_key are both missing"
      echo "[ERROR] TB_ANTHROPIC_AUTH_TOKEN is empty and TB_LLM_KWARGS.api_key is missing" >&2
      exit 1
    fi
  fi

  if ! harbor_runner_cli_ready; then
    harbor_validate_runner_cli
  fi

  local cmd=(
    "$HARBOR_OPIK_BIN" harbor run
    -y
    --n-concurrent "$TB_N_CONCURRENT"
    --max-retries "$TB_MAX_RETRIES"
    -o "$out_dir"
    -k "$TB_RUNS"
    --ak "version=$CLAUDE_CODE_VERSION"
    --ak "disallowed_tools=$TB_DISALLOWED_TOOLS"
    --ak "append_system_prompt=$TB_APPEND_SYSTEM_PROMPT"
    --ak "api_base=$TB_API_BASE"
    --ak "llm_kwargs=$normalized_llm_kwargs"
    --ak "max_new_tokens=$TB_MAX_NEW_TOKENS"
    --ak "model_info=$TB_MODEL_INFO"
    --ae "ANTHROPIC_BASE_URL=$TB_ANTHROPIC_BASE_URL"
    --ae "ANTHROPIC_AUTH_TOKEN=$TB_ANTHROPIC_AUTH_TOKEN"
    --ae "ANTHROPIC_CUSTOM_HEADERS=$TB_ANTHROPIC_CUSTOM_HEADERS"
    --ae "ANTHROPIC_MODEL=$TB_ANTHROPIC_MODEL"
    --ae "ANTHROPIC_DEFAULT_OPUS_MODEL=$TB_ANTHROPIC_DEFAULT_OPUS_MODEL"
    --ae "ANTHROPIC_DEFAULT_SONNET_MODEL=$TB_ANTHROPIC_DEFAULT_SONNET_MODEL"
    --ae "ANTHROPIC_DEFAULT_HAIKU_MODEL=$TB_ANTHROPIC_DEFAULT_HAIKU_MODEL"
    --ae "CLAUDE_CODE_SUBAGENT_MODEL=$TB_CLAUDE_CODE_SUBAGENT_MODEL"
    --ae "CLAUDE_CODE_EFFORT_LEVEL=$TB_CLAUDE_CODE_EFFORT_LEVEL"
    --ae "CLAUDE_CODE_MAX_OUTPUT_TOKENS=$TB_CLAUDE_CODE_MAX_OUTPUT_TOKENS"
    --ae "CLAUDE_CODE_DISABLE_AUTOUPDATER=$TB_CLAUDE_CODE_DISABLE_AUTOUPDATER"
    --ae "TRACE_TO_OPIK=$TRACE_TO_OPIK"
    --ae "CC_OPIK_DEBUG=$TB_CC_OPIK_DEBUG"
    --ae "CC_OPIK_INSTALL_DEPS=$TB_CC_OPIK_INSTALL_DEPS"
    --ae "CC_OPIK_HOOK_MOUNT_PATH=$TB_CC_HOOK_MOUNT_PATH"
    --ae "CC_OPIK_CLAUDE_TGZ_PATH=$TB_CC_CLAUDE_TGZ_MOUNT_PATH"
    --ae "CC_OPIK_PY_WHEEL_DIR=$TB_CC_PY_WHEEL_DIR_MOUNT_PATH"
    --ae "CC_OPIK_NPM_CACHE_DIR=$TB_CC_NPM_CACHE_MOUNT_PATH"
    --ae "TB_LOCAL_WHEEL_SERVER_URL=${TB_LOCAL_WHEEL_SERVER_URL:-}"
    --ae "TB_LOCAL_CLAUDE_TGZ_URL=${TB_LOCAL_CLAUDE_TGZ_URL:-}"
    --ae "TB_LOCAL_WHEEL_PORT=$LOCAL_WHEEL_PORT"
    --ae "PIP_DEFAULT_TIMEOUT=$TB_PIP_DEFAULT_TIMEOUT"
    --ae "PIP_RETRIES=$TB_PIP_RETRIES"
    --ae "PIP_DISABLE_PIP_VERSION_CHECK=1"
    --ae "TB_RUN_ID=${TB_RUN_ID:-$job_name}"
    --ae "TB_TASK_ID=$effective_tb_task_id"
    --ae "TB_INCLUDE_TASKS=${TB_INCLUDE_TASKS:-$INCLUDE_TASKS}"
    --ae "INCLUDE_TASKS=$INCLUDE_TASKS"
    --timeout-multiplier "$TB_TIMEOUT_MULTIPLIER"
    --agent-setup-timeout-multiplier "$TB_AGENT_SETUP_TIMEOUT_MULTIPLIER"
  )
  # Task code can read its own environment. With tracing disabled nothing in
  # the container consumes the Opik connection fields, so do not expose the
  # endpoint or credentials there.
  if harbor_trace_to_opik_enabled; then
    cmd+=(
      --ae "OPIK_URL_OVERRIDE=$OPIK_URL_OVERRIDE"
      --ae "OPIK_URL=$OPIK_URL_OVERRIDE"
      --ae "OPIK_PROJECT_NAME=$OPIK_PROJECT_NAME"
      --ae "OPIK_API_KEY=$OPIK_API_KEY"
      --ae "OPIK_WORKSPACE=$OPIK_WORKSPACE"
    )
  fi
  if harbor_uses_registry_dataset; then
    cmd+=( --dataset "$(harbor_registry_dataset_name)" )
  else
    cmd+=( --path "$TB_PATH" )
  fi
  append_harbor_unprivileged_docker_compose
  if [[ -n "${TB_VERIFIER_UV_HOME:-}" ]]; then
    cmd+=( --ve "HOME=$TB_VERIFIER_UV_HOME" )
  fi

  if [[ -n "${TB_AGENT_TIMEOUT_MULTIPLIER:-}" ]]; then
    cmd+=( --agent-timeout-multiplier "$TB_AGENT_TIMEOUT_MULTIPLIER" )
  fi

  if [[ -n "$TB_AK_MAX_TURNS" ]]; then
    cmd+=( --ak "max_turns=$TB_AK_MAX_TURNS" )
  fi
  if [[ -n "$TB_AK_COLLECT_ROLLOUT_DETAILS" ]]; then
    cmd+=( --ak "collect_rollout_details=$TB_AK_COLLECT_ROLLOUT_DETAILS" )
  fi
  if [[ -n "$TB_AK_ENABLE_SUMMARIZE" ]]; then
    cmd+=( --ak "enable_summarize=$TB_AK_ENABLE_SUMMARIZE" )
  fi

  local opik_host wheel_host no_proxy_value
  opik_host="$(
    python3 - "$OPIK_URL_OVERRIDE" <<'PY'
from urllib.parse import urlparse
import sys

u = urlparse(sys.argv[1])
print(u.hostname or "")
PY
  )"
  wheel_host="$(
    python3 - "${TB_LOCAL_WHEEL_SERVER_URL:-}" <<'PY'
from urllib.parse import urlparse
import sys

u = urlparse(sys.argv[1] if len(sys.argv) > 1 else "")
print(u.hostname or "")
PY
  )"
  no_proxy_value="127.0.0.1,localhost,host.docker.internal"
  if [[ -n "$opik_host" ]]; then
    no_proxy_value="$no_proxy_value,$opik_host"
  fi
  if [[ -n "$wheel_host" ]]; then
    no_proxy_value="$no_proxy_value,$wheel_host"
  fi
  cmd+=( --ae "NO_PROXY=$no_proxy_value" --ae "no_proxy=$no_proxy_value" )

  local hook_mount_enabled=0
  # The hook has no Opik server to talk to when tracing is off, so an
  # exported TB_CC_OPIK_ENABLE_HOOK=1 (e.g. persisted by setup.sh) must not
  # re-enable it.
  if [[ "$TB_CC_OPIK_ENABLE_HOOK" == "1" ]] && harbor_trace_to_opik_enabled; then
    if [[ -f "$TB_CC_HOOK_SOURCE" ]]; then
      hook_mount_enabled=1
      cmd+=( --ae "CC_OPIK_ENABLE_HOOK=true" )
    else
      echo "[WARN] CC hook source not found, disable realtime hook: $TB_CC_HOOK_SOURCE"
      cmd+=( --ae "CC_OPIK_ENABLE_HOOK=false" )
    fi
  else
    cmd+=( --ae "CC_OPIK_ENABLE_HOOK=false" )
  fi

  local mounts_json
  mounts_json="$(
    python3 - "$hook_mount_enabled" "$TB_CC_HOOK_SOURCE" "$TB_CC_HOOK_MOUNT_PATH" "$TB_CC_CLAUDE_TGZ_SOURCE" "$TB_CC_CLAUDE_TGZ_MOUNT_PATH" "$TB_CC_PY_WHEEL_DIR_SOURCE" "$TB_CC_PY_WHEEL_DIR_MOUNT_PATH" "$VERIFIER_UV_BIN_DIR_SOURCE" "$TB_VERIFIER_UV_BIN_DIR_MOUNT_PATH" <<'PY'
import json
import os
import sys

hook_enabled = sys.argv[1] == "1"
src = sys.argv[2]
dst = sys.argv[3]
claude_src = sys.argv[4]
claude_dst = sys.argv[5]
wheel_src = sys.argv[6]
wheel_dst = sys.argv[7]
uv_src = sys.argv[8]
uv_dst = sys.argv[9]
mounts = []
def bind_mount(src, dst):
    return {"type": "bind", "source": src, "target": dst, "read_only": True}
# Only the hook source follows the hook switch. The Claude package and
# wheel/runtime caches serve the offline agent install and must stay
# mounted with tracing disabled, for benchmark and rollout workers alike.
# NOTE: no single quotes in this heredoc; bash 3.2 mis-parses them inside
# command substitution.
if hook_enabled:
    mounts.append(bind_mount(src, dst))
if claude_src and os.path.exists(claude_src):
    mounts.append(bind_mount(claude_src, claude_dst))
if wheel_src and os.path.exists(wheel_src):
    mounts.append(bind_mount(wheel_src, wheel_dst))
if (
    uv_src
    and os.path.isdir(uv_src)
    and os.path.exists(os.path.join(uv_src, "uv"))
    and os.path.exists(os.path.join(uv_src, "uvx"))
):
    mounts.append(bind_mount(uv_src, uv_dst))
print(json.dumps(mounts, ensure_ascii=True))
PY
  )"
  if [[ "$mounts_json" != "[]" ]]; then
    cmd+=( --mounts-json "$mounts_json" )
  fi
  if [[ -n "$VERIFIER_UV_BIN_DIR_SOURCE" && -x "$VERIFIER_UV_BIN_DIR_SOURCE/uv" && -x "$VERIFIER_UV_BIN_DIR_SOURCE/uvx" ]]; then
    local verifier_uv_path_prefix
    verifier_uv_path_prefix="/root/.local/bin:/home/oai/.local/bin:/home/agent/.local/bin:/home/ubuntu/.local/bin"
    if [[ -n "${TB_VERIFIER_UV_HOME:-}" ]]; then
      verifier_uv_path_prefix="$TB_VERIFIER_UV_HOME/.local/bin:$verifier_uv_path_prefix"
    fi
    cmd+=(
      --ve "PATH=$verifier_uv_path_prefix:$TB_VERIFIER_UV_BIN_DIR_MOUNT_PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    )
  fi

  if [[ -n "${PIP_INDEX_URL:-}" ]]; then
    cmd+=( --ae "PIP_INDEX_URL=$PIP_INDEX_URL" --ve "PIP_INDEX_URL=$PIP_INDEX_URL" )
  fi
  if [[ -n "${PIP_EXTRA_INDEX_URL:-}" ]]; then
    cmd+=( --ae "PIP_EXTRA_INDEX_URL=$PIP_EXTRA_INDEX_URL" --ve "PIP_EXTRA_INDEX_URL=$PIP_EXTRA_INDEX_URL" )
  fi
  if [[ -n "${PIP_TRUSTED_HOST:-}" ]]; then
    cmd+=( --ae "PIP_TRUSTED_HOST=$PIP_TRUSTED_HOST" --ve "PIP_TRUSTED_HOST=$PIP_TRUSTED_HOST" )
  fi
  if [[ -n "${UV_INDEX_URL:-}" ]]; then
    # SWE-smith verifier scripts run `uv add ...`; pip env alone is ignored by uv.
    cmd+=( --ae "UV_INDEX_URL=$UV_INDEX_URL" --ve "UV_INDEX_URL=$UV_INDEX_URL" )
  fi
  if [[ -n "${UV_DEFAULT_INDEX:-}" ]]; then
    cmd+=( --ae "UV_DEFAULT_INDEX=$UV_DEFAULT_INDEX" --ve "UV_DEFAULT_INDEX=$UV_DEFAULT_INDEX" )
  fi
  if [[ -n "${NPM_CONFIG_REGISTRY:-}" ]]; then
    cmd+=( --ae "NPM_CONFIG_REGISTRY=$NPM_CONFIG_REGISTRY" --ve "NPM_CONFIG_REGISTRY=$NPM_CONFIG_REGISTRY" )
  fi
  if [[ -n "${GO111MODULE:-}" ]]; then
    cmd+=( --ae "GO111MODULE=$GO111MODULE" --ve "GO111MODULE=$GO111MODULE" )
  fi
  if [[ -n "${GOPROXY:-}" ]]; then
    cmd+=( --ae "GOPROXY=$GOPROXY" --ve "GOPROXY=$GOPROXY" )
  fi
  if [[ -n "${GOSUMDB:-}" ]]; then
    cmd+=( --ae "GOSUMDB=$GOSUMDB" --ve "GOSUMDB=$GOSUMDB" )
  fi
  append_rust_package_mirror_env --ae
  append_rust_package_mirror_env --ve

  if [[ "$TB_DEBUG" == "1" ]]; then
    cmd+=( --debug )
  fi

  if [[ -n "$TB_AGENT" ]]; then
    cmd+=( -a "$TB_AGENT" )
  fi

  if [[ -n "$TB_AGENT_IMPORT_PATH" ]]; then
    cmd+=( --agent-import-path "$TB_AGENT_IMPORT_PATH" )
  fi

  if [[ -n "$TB_MODEL" ]]; then
    cmd+=( -m "$TB_MODEL" )
  fi

  if [[ -n "$TB_LIMIT" ]]; then
    cmd+=( -l "$TB_LIMIT" )
  fi

  if [[ -n "$INCLUDE_TASKS" ]]; then
    IFS=',' read -r -a include_arr <<< "$INCLUDE_TASKS"
    for task_name in "${include_arr[@]}"; do
      task_name="${task_name#"${task_name%%[![:space:]]*}"}"
      task_name="${task_name%"${task_name##*[![:space:]]}"}"
      if [[ -n "$task_name" ]]; then
        cmd+=( -i "$(harbor_registry_task_name "$task_name")" )
      fi
    done
  fi

  if [[ -n "$TB_RETRY_INCLUDE_EXCEPTIONS" ]]; then
    IFS=',' read -r -a retry_include_arr <<< "$TB_RETRY_INCLUDE_EXCEPTIONS"
    for exception_name in "${retry_include_arr[@]}"; do
      exception_name="${exception_name#"${exception_name%%[![:space:]]*}"}"
      exception_name="${exception_name%"${exception_name##*[![:space:]]}"}"
      if [[ -n "$exception_name" ]]; then
        cmd+=( --retry-include "$exception_name" )
      fi
    done
  fi

  if [[ -n "$TB_RETRY_EXCLUDE_EXCEPTIONS" ]]; then
    IFS=',' read -r -a retry_exclude_arr <<< "$TB_RETRY_EXCLUDE_EXCEPTIONS"
    for exception_name in "${retry_exclude_arr[@]}"; do
      exception_name="${exception_name#"${exception_name%%[![:space:]]*}"}"
      exception_name="${exception_name%"${exception_name##*[![:space:]]}"}"
      if [[ -n "$exception_name" ]]; then
        cmd+=( --retry-exclude "$exception_name" )
      fi
    done
  fi

  if [[ "${TB_FORCE_BUILD:-0}" == "1" || "${TB_FORCE_BUILD:-0}" == "true" ]]; then
    # Some datasets publish prebuilt task images, but registry mirrors can return
    # 429/not-found. Force-build bypasses those prebuilt pulls when needed.
    cmd+=( --force-build )
  fi

  echo "[INFO] running TB with real-time Opik tracking"
  echo "[INFO] project: $OPIK_PROJECT_NAME"
  if harbor_uses_registry_dataset; then
    echo "[INFO] agent: $TB_AGENT | runs: $TB_RUNS | dataset: $(harbor_registry_dataset_name)"
  else
    echo "[INFO] agent: $TB_AGENT | runs: $TB_RUNS | path: $TB_PATH"
  fi
  echo "[INFO] agent_import_path: ${TB_AGENT_IMPORT_PATH:-<none>}"
  echo "[INFO] output dir: $out_dir"
  echo "[INFO] dashboard: ${OPIK_BASE%/}/${OPIK_WORKSPACE}/home"
  echo "[INFO] model: $TB_MODEL"
  echo "[INFO] claude max_turns: ${TB_AK_MAX_TURNS:-<default>}"
  echo "[INFO] n_concurrent: $TB_N_CONCURRENT | max_retries: $TB_MAX_RETRIES"
  echo "[INFO] retry_include_exceptions: ${TB_RETRY_INCLUDE_EXCEPTIONS:-<all-except-excludes>}"
  echo "[INFO] retry_exclude_exceptions: ${TB_RETRY_EXCLUDE_EXCEPTIONS:-<none>}"
  echo "[INFO] realtime_hook_enabled: $TB_CC_OPIK_ENABLE_HOOK | hook_source: $TB_CC_HOOK_SOURCE"
  echo "[INFO] pip_index_url: ${PIP_INDEX_URL:-<default>} | pip_timeout: $TB_PIP_DEFAULT_TIMEOUT | pip_retries: $TB_PIP_RETRIES"
  echo "[INFO] api_base: ${TB_API_BASE:-<empty>}"
  echo "[INFO] timeout_multiplier: $TB_TIMEOUT_MULTIPLIER | agent_setup_timeout_multiplier: $TB_AGENT_SETUP_TIMEOUT_MULTIPLIER"
  echo "[INFO] disallowed_tools: $TB_DISALLOWED_TOOLS"
  echo "[INFO] append_system_prompt configured: yes"
  if [[ "$normalized_llm_kwargs" == *'"api_key":"="'* || "$normalized_llm_kwargs" == *'"api_key": "="'* ]]; then
    echo "[WARN] llm_kwargs is using placeholder api_key='='; this often yields all-zero scores"
  fi
  echo "[INFO] harbor cmd: $HARBOR_OPIK_BIN harbor run ..."

  if [[ "$TB_DRY_RUN" == "1" ]]; then
    echo "[INFO] TB_DRY_RUN=1, skip execution"
    return 0
  fi

  export PYTHONPATH="$HARBOR_CLAUDE_CODE_DIR:$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"
  "${cmd[@]}"

  echo "[INFO] completed"
  echo "[INFO] results: $out_dir"
  echo "[INFO] open traces in Opik project: $OPIK_PROJECT_NAME"
}

run_opencode_task() {
  harbor_apply_effective_wheel_source
  if harbor_trace_to_opik_enabled; then
    normalize_opik_url_override
  fi

  if [[ "$TB_DRY_RUN" != "1" ]] && ! harbor_runner_cli_ready; then
    harbor_validate_runner_cli
  fi
  if [[ "$TB_DRY_RUN" != "1" && ! -x "$HARBOR_OPIK_PYTHON" ]]; then
    echo "[ERROR] HARBOR_OPIK_PYTHON not executable: $HARBOR_OPIK_PYTHON" >&2
    echo "[ERROR] set HARBOR_OPIK_PYTHON to the Python inside the Harbor runner environment" >&2
    exit 1
  fi

  if [[ "$TB_DRY_RUN" != "1" && "$OPIK_MODE" == "remote" ]] && harbor_trace_to_opik_enabled; then
    verify_opik_reachable
    verify_opik_ingestion_route
  fi

  mkdir -p "$JOBS_ROOT"
  local job_name out_dir
  job_name="$(date +%Y-%m-%d__%H-%M-%S)"
  out_dir="$JOBS_ROOT/$job_name"
  if harbor_is_native_registry_main; then
    printf '%s\n' "$out_dir" > "$HARBOR_JOB_DIR_FILE"
  fi
  mkdir -p "$out_dir"

  local effective_tb_task_id include_task
  effective_tb_task_id="${TB_TASK_ID:-}"
  if [[ -z "$effective_tb_task_id" ]]; then
    include_task="$(single_include_task "${INCLUDE_TASKS:-${TB_INCLUDE_TASKS:-}}" || true)"
    if [[ -n "$include_task" ]]; then
      effective_tb_task_id="$include_task"
    fi
  fi

  local opik_host no_proxy_value
  opik_host="$(
    python3 - "$OPIK_URL_OVERRIDE" <<'PY'
from urllib.parse import urlparse
import sys
print(urlparse(sys.argv[1]).hostname or "")
PY
  )"
  no_proxy_value="127.0.0.1,localhost,host.docker.internal"
  if [[ -n "$opik_host" ]]; then
    no_proxy_value="$no_proxy_value,$opik_host"
  fi

  local cmd
  build_opencode_cmd() {
    local trial_id="$1"
    local opencode_tgz_url=""
    local opencode_linux_x64_tgz_url=""
    if [[ -n "${TB_LOCAL_WHEEL_SERVER_URL:-}" ]]; then
      opencode_tgz_url="${TB_LOCAL_WHEEL_SERVER_URL%/}/${OPENCODE_TGZ_BASENAME}"
      opencode_linux_x64_tgz_url="${TB_LOCAL_WHEEL_SERVER_URL%/}/${OPENCODE_LINUX_X64_TGZ_BASENAME}"
    fi
    cmd=(
      "$HARBOR_OPIK_PYTHON" "$HARBOR_OPENCODE_DIR/enable_track_harbor.py" run
      -y
      --n-concurrent 1
      --max-retries "$TB_MAX_RETRIES"
      -o "$out_dir"
      -k 1
      --ak "version=$OPENCODE_VERSION"
      --agent-import-path opik_opencode_harbor:OpikOpenCodeHarbor
      -m "$TB_MODEL"
      --ae "TRACE_TO_OPIK=$TRACE_TO_OPIK"
      --ae "CC_OPIK_PY_WHEEL_DIR=$TB_CC_PY_WHEEL_DIR_MOUNT_PATH"
      --ae "TB_LOCAL_WHEEL_SERVER_URL=${TB_LOCAL_WHEEL_SERVER_URL:-}"
      --ae "OPENCODE_TGZ_PATH=$TB_CC_PY_WHEEL_DIR_MOUNT_PATH/$OPENCODE_TGZ_BASENAME"
      --ae "OPENCODE_LINUX_X64_TGZ_PATH=$TB_CC_PY_WHEEL_DIR_MOUNT_PATH/$OPENCODE_LINUX_X64_TGZ_BASENAME"
      --ae "TB_LOCAL_OPENCODE_TGZ_URL=$opencode_tgz_url"
      --ae "TB_LOCAL_OPENCODE_LINUX_X64_TGZ_URL=$opencode_linux_x64_tgz_url"
      --ae "TB_RUN_ID=${TB_RUN_ID:-$RUN_ID}"
      --ae "TB_TASK_ID=$effective_tb_task_id"
      --ae "TB_INCLUDE_TASKS=${TB_INCLUDE_TASKS:-$INCLUDE_TASKS}"
      --ae "INCLUDE_TASKS=$INCLUDE_TASKS"
      --ae "TB_TRIAL_ID=$trial_id"
      --ae "NO_PROXY=$no_proxy_value"
      --ae "no_proxy=$no_proxy_value"
      --timeout-multiplier "$TB_TIMEOUT_MULTIPLIER"
      --agent-setup-timeout-multiplier "$TB_AGENT_SETUP_TIMEOUT_MULTIPLIER"
    )
    # Same rule as the Claude builder: no Opik endpoint or credentials in
    # trace-off task environments.
    if harbor_trace_to_opik_enabled; then
      cmd+=(
        --ae "OPIK_URL_OVERRIDE=$OPIK_URL_OVERRIDE"
        --ae "OPIK_URL=$OPIK_URL_OVERRIDE"
        --ae "OPIK_PROJECT_NAME=$OPIK_PROJECT_NAME"
        --ae "OPIK_API_KEY=$OPIK_API_KEY"
        --ae "OPIK_WORKSPACE=$OPIK_WORKSPACE"
      )
    fi
    if harbor_uses_registry_dataset; then
      cmd+=( --dataset "$(harbor_registry_dataset_name)" )
    else
      cmd+=( --path "$TB_PATH" )
    fi
    append_harbor_unprivileged_docker_compose

    if [[ -n "${TB_AGENT_TIMEOUT_MULTIPLIER:-}" ]]; then
      cmd+=( --agent-timeout-multiplier "$TB_AGENT_TIMEOUT_MULTIPLIER" )
    fi

    if [[ -n "${OPENCODE_CONFIG_CONTENT:-}" ]]; then
      cmd+=( --ak "opencode_config=$OPENCODE_CONFIG_CONTENT" )
    else
      if [[ -n "${TB_ANTHROPIC_BASE_URL:-}" ]]; then
        cmd+=( --ae "ANTHROPIC_BASE_URL=$TB_ANTHROPIC_BASE_URL" )
      fi
      if [[ -n "${TB_ANTHROPIC_AUTH_TOKEN:-}" ]]; then
        cmd+=( --ae "ANTHROPIC_AUTH_TOKEN=$TB_ANTHROPIC_AUTH_TOKEN" )
        cmd+=( --ae "ANTHROPIC_API_KEY=$TB_ANTHROPIC_AUTH_TOKEN" )
      fi
    fi

    local mounts_json
    mounts_json="$(
      python3 - "$TB_CC_PY_WHEEL_DIR_SOURCE" "$TB_CC_PY_WHEEL_DIR_MOUNT_PATH" <<'PY'
import json
import os
import sys

src = sys.argv[1]
dst = sys.argv[2]
mounts = []
if src and os.path.exists(src):
    mounts.append({
        "type": "bind",
        "source": src,
        "target": dst,
        "read_only": True,
    })
print(json.dumps(mounts, ensure_ascii=True))
PY
    )"
    if [[ "$mounts_json" != "[]" ]]; then
      cmd+=( --mounts-json "$mounts_json" )
    fi

    for env_name in OC_OPIK_DEBUG OC_OPIK_DRY_RUN OC_OPIK_MAX_TEXT_CHARS OC_OPIK_FLUSH_INTERVAL_S; do
      if [[ -n "${!env_name:-}" ]]; then
        cmd+=( --ae "${env_name}=${!env_name}" )
      fi
    done

    if [[ -n "${PIP_INDEX_URL:-}" ]]; then
      cmd+=( --ae "PIP_INDEX_URL=$PIP_INDEX_URL" --ve "PIP_INDEX_URL=$PIP_INDEX_URL" )
    fi
    if [[ -n "${PIP_EXTRA_INDEX_URL:-}" ]]; then
      cmd+=( --ae "PIP_EXTRA_INDEX_URL=$PIP_EXTRA_INDEX_URL" --ve "PIP_EXTRA_INDEX_URL=$PIP_EXTRA_INDEX_URL" )
    fi
    if [[ -n "${PIP_TRUSTED_HOST:-}" ]]; then
      cmd+=( --ae "PIP_TRUSTED_HOST=$PIP_TRUSTED_HOST" --ve "PIP_TRUSTED_HOST=$PIP_TRUSTED_HOST" )
    fi
    if [[ -n "${UV_INDEX_URL:-}" ]]; then
      cmd+=( --ae "UV_INDEX_URL=$UV_INDEX_URL" --ve "UV_INDEX_URL=$UV_INDEX_URL" )
    fi
    if [[ -n "${UV_DEFAULT_INDEX:-}" ]]; then
      cmd+=( --ae "UV_DEFAULT_INDEX=$UV_DEFAULT_INDEX" --ve "UV_DEFAULT_INDEX=$UV_DEFAULT_INDEX" )
    fi
    if [[ -n "${NPM_CONFIG_REGISTRY:-}" ]]; then
      cmd+=( --ae "NPM_CONFIG_REGISTRY=$NPM_CONFIG_REGISTRY" --ve "NPM_CONFIG_REGISTRY=$NPM_CONFIG_REGISTRY" )
    fi
    if [[ -n "${GO111MODULE:-}" ]]; then
      cmd+=( --ae "GO111MODULE=$GO111MODULE" --ve "GO111MODULE=$GO111MODULE" )
    fi
    if [[ -n "${GOPROXY:-}" ]]; then
      cmd+=( --ae "GOPROXY=$GOPROXY" --ve "GOPROXY=$GOPROXY" )
    fi
    if [[ -n "${GOSUMDB:-}" ]]; then
      cmd+=( --ae "GOSUMDB=$GOSUMDB" --ve "GOSUMDB=$GOSUMDB" )
    fi
    append_rust_package_mirror_env --ae
    append_rust_package_mirror_env --ve

    if [[ -n "$INCLUDE_TASKS" ]]; then
      IFS=',' read -r -a include_arr <<< "$INCLUDE_TASKS"
      for task_name in "${include_arr[@]}"; do
        task_name="${task_name#"${task_name%%[![:space:]]*}"}"
        task_name="${task_name%"${task_name##*[![:space:]]}"}"
        if [[ -n "$task_name" ]]; then
          # Harbor selects tasks in the outer CLI. Passing INCLUDE_TASKS only as
          # agent env is too late and makes one worker run many tasks.
          cmd+=( -i "$(harbor_registry_task_name "$task_name")" )
        fi
      done
    fi

    if [[ "$TB_DEBUG" == "1" ]]; then
      cmd+=( --debug )
    fi
  }

  echo "[INFO] opencode run attempts=$N_ATTEMPTS"
  echo "[INFO] project: $OPIK_PROJECT_NAME"
  echo "[INFO] output dir: $out_dir"
  if harbor_uses_registry_dataset; then
    echo "[INFO] dataset: $(harbor_registry_dataset_name)"
  else
    echo "[INFO] path: $TB_PATH"
  fi
  echo "[INFO] model: $TB_MODEL"
  echo "[INFO] opencode version: $OPENCODE_VERSION"

  export PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"

  local overall_rc=0
  local attempt trial_id rc
  for ((attempt = 1; attempt <= N_ATTEMPTS; attempt++)); do
    trial_id="attempt-${attempt}"
    build_opencode_cmd "$trial_id"
    echo "[INFO] attempt $attempt/$N_ATTEMPTS trial_id=$trial_id"
    echo "[INFO] harbor cmd: $HARBOR_OPIK_PYTHON $HARBOR_OPENCODE_DIR/enable_track_harbor.py run ..."

    if [[ "$TB_DRY_RUN" == "1" ]]; then
      printf '  %s\n' "${cmd[@]}"
      continue
    fi

    set +e
    "${cmd[@]}"
    rc=$?
    set -e
    if [[ $rc -ne 0 ]]; then
      overall_rc=$rc
      echo "[WARN] attempt $attempt failed (rc=$overall_rc); continuing remaining attempts" >&2
    fi
  done

  return "$overall_rc"
}

main() {
  harbor_validate_agent
  configure_trace_disabled_runtime
  if harbor_agent_is_opencode; then
    need_cmd curl
    need_cmd python3
    if harbor_trace_to_opik_enabled; then
      normalize_opik_url_override
    fi
    ensure_trace_plugin_source_if_needed
    apply_min_test_defaults

    if [[ "$TB_DRY_RUN" == "1" ]]; then
      echo "[INFO] TB_DRY_RUN=1, skip dataset/opik readiness checks"
      run_opencode_task
      return $?
    fi

    ensure_docker_daemon
    docker_hub_preflight_check

    if ! harbor_trace_to_opik_enabled; then
      echo "[INFO] TRACE_TO_OPIK=false, skip Opik readiness checks"
    else
      if [[ "$OPIK_MODE" != "local" && "$OPIK_MODE" != "remote" ]]; then
        echo "[ERROR] OPIK_MODE must be local or remote, got: $OPIK_MODE" >&2
        exit 1
      fi

      if [[ "$OPIK_MODE" == "local" ]]; then
        ensure_opik_repo
      fi
    fi
    prepare_local_dataset_if_needed
    if harbor_trace_to_opik_enabled; then
      if [[ "$OPIK_MODE" == "local" ]]; then
        start_opik_local
      elif [[ "$OPIK_BASE" == "http://localhost:5173" && "$OPIK_URL_OVERRIDE" == "http://localhost:5173/api" ]]; then
        echo "[ERROR] OPIK_MODE=remote requires a real remote Opik endpoint." >&2
        echo "[ERROR] please set OPIK_BASE (for example: https://your-opik-host)" >&2
        echo "[ERROR] and optionally OPIK_URL_OVERRIDE (for example: https://your-opik-host/api)." >&2
        exit 1
      fi
    fi

    run_opencode_task
    return $?
  fi

  need_cmd git
  need_cmd curl
  need_cmd python3
  ensure_docker_daemon
  if harbor_trace_to_opik_enabled; then
    normalize_opik_url_override
  fi
  ensure_trace_plugin_source_if_needed

  if [[ -z "$TB_AGENT" && -z "$TB_AGENT_IMPORT_PATH" ]]; then
    echo "[ERROR] at least one of TB_AGENT or TB_AGENT_IMPORT_PATH must be set" >&2
    exit 1
  fi

  if [[ -z "$TB_AGENT_IMPORT_PATH" && "$TB_AGENT" != "claude-code" ]]; then
    echo "[ERROR] when TB_AGENT_IMPORT_PATH is empty, TB_AGENT must be claude-code (got: $TB_AGENT)" >&2
    exit 1
  fi

  apply_min_test_defaults

  if [[ "$TB_DRY_RUN" == "1" ]]; then
    echo "[INFO] TB_DRY_RUN=1, skip dataset/opik readiness checks"
    run_tb
    return 0
  fi

  docker_hub_preflight_check

  if ! harbor_trace_to_opik_enabled; then
    echo "[INFO] TRACE_TO_OPIK=false, skip Opik readiness checks"
  else
    if [[ "$OPIK_MODE" != "local" && "$OPIK_MODE" != "remote" ]]; then
      echo "[ERROR] OPIK_MODE must be local or remote, got: $OPIK_MODE" >&2
      exit 1
    fi

    if [[ "$OPIK_MODE" == "local" ]]; then
      ensure_opik_repo
    fi
  fi
  prepare_local_dataset_if_needed
  if harbor_trace_to_opik_enabled; then
    if [[ "$OPIK_MODE" == "local" ]]; then
      start_opik_local
    else
      if [[ "$OPIK_BASE" == "http://localhost:5173" && "$OPIK_URL_OVERRIDE" == "http://localhost:5173/api" ]]; then
        echo "[ERROR] OPIK_MODE=remote requires a real remote Opik endpoint." >&2
        echo "[ERROR] please set OPIK_BASE (for example: https://your-opik-host)" >&2
        echo "[ERROR] and optionally OPIK_URL_OVERRIDE (for example: https://your-opik-host/api)." >&2
        exit 1
      fi
      verify_opik_reachable
      verify_opik_ingestion_route
    fi
  fi
  run_tb
}

main
