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
#   3. Apply minimal-test defaults when MIN_TEST=1 (fast smoke test)
#   4. Docker Hub connectivity preflight (warn or abort if unreachable)
#   5. Verify Opik health and ingestion endpoints when OPIK_URL is set
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
#   MIN_TEST=1 bash harboropik.sh                       # quick smoke test
#   HARBOR_DRY_RUN=1  bash harboropik.sh                    # print command, skip run
#   OPIK_URL=http://host:5173/api \
#     HARBOR_RUNS=10 HARBOR_N_CONCURRENT=4 bash harboropik.sh   # standard remote run

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/env.sh"

harbor_is_native_registry_main() {
  [[ "$ROLLOUT" != "1" ]] \
    && harbor_uses_registry_dataset \
    && [[ "${HARBOR_QUEUE_WORKER:-0}" != "1" ]]
}

harbor_is_fixer_verification_main() {
  [[ "${HARBOR_FIXER_VERIFICATION_RERUN:-0}" == "1" ]] \
    && [[ "${HARBOR_QUEUE_WORKER:-0}" != "1" ]]
}

harbor_publishes_job_dir() {
  harbor_is_native_registry_main \
    || harbor_is_fixer_verification_main
}

harbor_uses_local_opensandbox_dataset() {
  [[ "$HARBOR_ENVIRONMENT_TYPE" == "opensandbox" ]] \
    && [[ -n "${DATASET_PATH:-}" ]] \
    && [[ -d "$DATASET_PATH" ]]
}

harbor_environment_supports_claude_hook_delivery() {
  [[ "$HARBOR_ENVIRONMENT_TYPE" == "docker" \
    || "$HARBOR_ENVIRONMENT_TYPE" == "opensandbox" ]]
}

write_harbor_registry_summary() {
  local exit_code="$1"
  local job_dir=""
  local dataset

  [[ "$HARBOR_DRY_RUN" != "1" ]] || return 0
  [[ -f "$HARBOR_JOB_DIR_FILE" ]] && job_dir="$(cat "$HARBOR_JOB_DIR_FILE" 2>/dev/null || true)"
  dataset="$(harbor_registry_dataset_name)"
  python3 "$SCRIPT_DIR/scripts/write_harbor_registry_summary.py" \
    "$job_dir" "$OUTPUT_PATH/summary.txt" "$exit_code" "$dataset"
}

if harbor_publishes_job_dir; then
  : > "$HARBOR_JOB_DIR_FILE"
  rm -f "$HARBOR_BENCHMARK_EXIT_FILE"
  # BASHPID needs bash >= 4; at this top-level scope $$ is the same pid.
  harbor_benchmark_pid="${BASHPID:-$$}"
  printf '%s\t%s\n' \
    "$harbor_benchmark_pid" \
    "$(awk '{print $22}' "/proc/$harbor_benchmark_pid/stat")" \
    > "$HARBOR_BENCHMARK_PID_FILE"
  record_harbor_benchmark_exit() {
    local rc="$?"
    # The exit file is the completion contract with the native monitor;
    # write it before the best-effort summary and analyzer teardown.
    printf '%s\n' "$rc" > "$HARBOR_BENCHMARK_EXIT_FILE"
    if harbor_is_native_registry_main; then
      if ! write_harbor_registry_summary "$rc"; then
        echo "[WARN] failed to write registry summary: $OUTPUT_PATH/summary.txt" >&2
      fi
    fi
    if ! harbor_stop_online_analysis; then
      echo "[WARN] failed to stop online analyzer for $OUTPUT_PATH" >&2
    fi
    if harbor_is_fixer_verification_main; then
      cleanup_verifier_uv_bin_dir
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
  python3 "$SCRIPT_DIR/harbor_shell_utils.py" online-event \
    "$phase" "$component" "$event" "$severity" "$fatal" "$message"
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
  export -n OPIK_URL OPIK_URL_OVERRIDE OPIK_BASE \
    OPIK_PROJECT_NAME OPIK_API_KEY OPIK_WORKSPACE
}

normalize_opik_url_override() {
  local normalized="${OPIK_URL_OVERRIDE%/}"
  if [[ -z "$normalized" ]]; then
    echo "[ERROR] OPIK_URL_OVERRIDE/OPIK_URL is empty; set it to an Opik API URL such as http://host:5173/api" >&2
    echo "[ERROR] or leave OPIK_URL empty to run the benchmark without Opik tracing" >&2
    exit 1
  fi
  if [[ "$normalized" != */api ]]; then
    OPIK_URL_OVERRIDE="${normalized}/api"
    echo "[WARN] OPIK_URL_OVERRIDE missing /api, auto-normalized to: $OPIK_URL_OVERRIDE"
  else
    OPIK_URL_OVERRIDE="$normalized"
  fi
  export OPIK_URL_OVERRIDE
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

append_package_environment_args() {
  local env_name
  for env_name in \
    PIP_INDEX_URL \
    PIP_EXTRA_INDEX_URL \
    PIP_TRUSTED_HOST \
    UV_INDEX_URL \
    UV_DEFAULT_INDEX \
    NPM_CONFIG_REGISTRY
  do
    if [[ -n "${!env_name:-}" ]]; then
      cmd+=( --ae "$env_name=${!env_name}" --ve "$env_name=${!env_name}" )
    fi
  done
  if [[ -n "${HARBOR_CC_NODE_DIST_URL:-}" ]]; then
    cmd+=( --ae "CC_NODE_DIST_URL=$HARBOR_CC_NODE_DIST_URL" )
  fi
  for env_name in GO111MODULE GOPROXY GOSUMDB; do
    if [[ -n "${!env_name:-}" ]]; then
      cmd+=( --ae "$env_name=${!env_name}" --ve "$env_name=${!env_name}" )
    fi
  done
  append_rust_package_mirror_env --ae
  append_rust_package_mirror_env --ve
}

append_harbor_unprivileged_docker_compose() {
  if [[ "${HARBOR_ENVIRONMENT_TYPE:-docker}" != "docker" ]]; then
    if [[ "${HARBOR_DRY_RUN:-0}" == "1" ]]; then
      echo "[INFO] ${HARBOR_ENVIRONMENT_TYPE} environment; skip unprivileged Docker Compose overlay"
    fi
    return 0
  fi
  cmd+=( --extra-docker-compose "$SCRIPT_DIR/overlays/unprivileged-task.yaml" )
  if [[ "${HARBOR_DRY_RUN:-0}" == "1" ]]; then
    echo "[INFO] Docker environment; add unprivileged Docker Compose overlay"
  fi
}

validate_environment_backend() {
  case "$HARBOR_ENVIRONMENT_TYPE" in
    docker)
      ;;
    e2b)
      if harbor_agent_is_pi; then
        echo "[ERROR] AGENT=pi with HARBOR_ENVIRONMENT_TYPE=$HARBOR_ENVIRONMENT_TYPE is unsupported: Pi's pinned Node/runtime archives and local extensions require host bind mounts." >&2
        echo "[ERROR] use HARBOR_ENVIRONMENT_TYPE=docker or opensandbox for AGENT=pi." >&2
        exit 1
      fi
      ;;
    opensandbox)
      local name
      for name in YICLOUD_PUBLIC_KEY YICLOUD_SECRET_KEY YICLOUD_PROJECT_NAME; do
        if [[ -z "${!name:-}" ]]; then
          echo "[ERROR] required environment variable is unset: $name" >&2
          exit 1
        fi
      done
      if [[ -z "$YICLOUD_HARBOR_HOST" ]]; then
        echo '[ERROR] required environment variable is unset: YICLOUD_HARBOR_HOST' >&2
        exit 1
      fi
      if [[ -z "${YICLOUD_SANDBOX_ENVIRONMENT_ID:-}" \
        && -z "${YICLOUD_SANDBOX_ENVIRONMENT_NAME:-}" ]]; then
        echo '[ERROR] OpenSandbox requires YICLOUD_SANDBOX_ENVIRONMENT_ID or YICLOUD_SANDBOX_ENVIRONMENT_NAME' >&2
        echo '[ERROR] refusing to create an instance without an explicit environment binding' >&2
        exit 1
      fi
      echo "[INFO] OpenSandbox environment binding: id=${YICLOUD_SANDBOX_ENVIRONMENT_ID:-<resolved-by-name>} name=${YICLOUD_SANDBOX_ENVIRONMENT_NAME:-<lookup-by-id>}"
      if ! resolve_opensandbox_task_image_ref; then
        exit 1
      fi
      if [[ -z "$HARBOR_OPENSANDBOX_IMAGE_REF" \
        && -z "$HARBOR_OPENSANDBOX_BUNDLE_MANIFEST" ]]; then
        if [[ -z "$YICLOUD_HARBOR_PROJECT" ]]; then
          echo "[ERROR] YICLOUD_HARBOR_PROJECT is required when HARBOR_OPENSANDBOX_IMAGE_REF is unset" >&2
          exit 1
        fi
        if [[ ! -f "$HARBOR_OPENSANDBOX_IMAGE_MANAGER" ]]; then
          echo "[ERROR] OpenSandbox image manager not found: $HARBOR_OPENSANDBOX_IMAGE_MANAGER" >&2
          exit 1
        fi
      fi
      ;;
    qz)
      if harbor_agent_is_pi; then
        echo "[ERROR] AGENT=pi with HARBOR_ENVIRONMENT_TYPE=$HARBOR_ENVIRONMENT_TYPE is unsupported: Pi's pinned Node/runtime archives and local extensions require host bind mounts." >&2
        echo "[ERROR] use HARBOR_ENVIRONMENT_TYPE=docker or opensandbox for AGENT=pi." >&2
        exit 1
      fi
      if [[ -z "${SBX_API_KEY:-}" && -z "${QZ_SANDBOX_API_KEY:-}" \
        && "${E2B_API_KEY:-}" != sbx_* ]]; then
        echo '[ERROR] qz sandbox requires SBX_API_KEY (or QZ_SANDBOX_API_KEY / an sbx_-prefixed E2B_API_KEY)' >&2
        exit 1
      fi
      if [[ -n "${QZ_SANDBOX_TEMPLATE:-}" \
        && -n "${QZ_SANDBOX_TEMPLATE_MAP:-}" ]]; then
        echo '[ERROR] set only one of QZ_SANDBOX_TEMPLATE or QZ_SANDBOX_TEMPLATE_MAP' >&2
        exit 1
      fi
      if [[ -z "${QZ_SANDBOX_TEMPLATE:-}" \
        && -z "${QZ_SANDBOX_TEMPLATE_MAP:-}" ]]; then
        echo '[ERROR] qz sandbox requires QZ_SANDBOX_TEMPLATE or QZ_SANDBOX_TEMPLATE_MAP' >&2
        exit 1
      fi
      if [[ -n "${QZ_SANDBOX_TEMPLATE_MAP:-}" \
        && ! -f "$QZ_SANDBOX_TEMPLATE_MAP" ]]; then
        echo "[ERROR] QZ_SANDBOX_TEMPLATE_MAP not found: $QZ_SANDBOX_TEMPLATE_MAP" >&2
        exit 1
      fi
      if [[ -n "${QZ_SANDBOX_TIMEOUT_SEC:-}" ]]; then
        if [[ ! "$QZ_SANDBOX_TIMEOUT_SEC" =~ ^[0-9]+$ ]] \
          || (( QZ_SANDBOX_TIMEOUT_SEC < 1 || QZ_SANDBOX_TIMEOUT_SEC > 14400 )); then
          echo "[ERROR] QZ_SANDBOX_TIMEOUT_SEC must be an integer between 1 and 14400 (the platform's 4-hour cap), got: $QZ_SANDBOX_TIMEOUT_SEC" >&2
          exit 1
        fi
      fi
      case "${HARBOR_FORCE_BUILD:-0}" in
        0|false|no|"") ;;
        *)
          echo '[ERROR] HARBOR_FORCE_BUILD is not supported on qz: templates are registered on the platform, not built by Harbor' >&2
          exit 1
          ;;
      esac
      if [[ -n "${QZ_SANDBOX_TEMPLATE:-}" ]]; then
        echo "[INFO] qz sandbox fixed template: $QZ_SANDBOX_TEMPLATE"
      else
        echo "[INFO] qz sandbox template map: $QZ_SANDBOX_TEMPLATE_MAP"
      fi
      if harbor_agent_is_claude_code; then
        echo "[INFO] qz claude-code delivery: npm registry ${NPM_CONFIG_REGISTRY:-<unset>} | node dist ${HARBOR_CC_NODE_DIST_URL:-<unset>}"
      elif harbor_agent_is_opencode; then
        echo "[INFO] qz opencode delivery: npm registry ${NPM_CONFIG_REGISTRY:-<unset>} | node dist ${HARBOR_CC_NODE_DIST_URL:-<unset>}"
      fi
      ;;
    *)
      echo "[ERROR] HARBOR_ENVIRONMENT_TYPE must be docker, e2b, opensandbox, or qz, got: $HARBOR_ENVIRONMENT_TYPE" >&2
      exit 1
      ;;
  esac

  if [[ -n "${HARBOR_E2B_SANDBOX_TIMEOUT_SEC:-}" ]] \
    && [[ ! "$HARBOR_E2B_SANDBOX_TIMEOUT_SEC" =~ ^[1-9][0-9]*$ ]]; then
    echo "[ERROR] HARBOR_E2B_SANDBOX_TIMEOUT_SEC must be a positive integer" >&2
    exit 1
  fi
}

opensandbox_task_image_ref() {
  local task_dir="${DATASET_PATH:-}/${INCLUDE_TASKS:-}"
  local task_config="$task_dir/task.toml"
  local parser_python="${HARBOR_OPIK_PYTHON:-}"
  [[ -f "$task_config" ]] || return 0
  if [[ -z "$parser_python" || ! -x "$parser_python" ]]; then
    parser_python="$(command -v python3 2>/dev/null || true)"
  fi
  if [[ -z "$parser_python" || ! -x "$parser_python" ]]; then
    echo "[ERROR] Python 3 is required to read $task_config" >&2
    return 1
  fi
  "$parser_python" -c '
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from qz_template_mapping import QzTemplateMappingError, load_task_image

try:
    print(load_task_image(Path(sys.argv[2])))
except QzTemplateMappingError as error:
    if "missing environment.docker_image" not in str(error):
        raise
' "$SCRIPT_DIR" "$task_dir"
}

resolve_opensandbox_task_image_ref() {
  if [[ "$HARBOR_ENVIRONMENT_TYPE" != "opensandbox" \
    || -n "$HARBOR_OPENSANDBOX_IMAGE_REF" \
    || -n "$HARBOR_OPENSANDBOX_BUNDLE_MANIFEST" ]]; then
    return 0
  fi
  local task_image_ref
  if ! task_image_ref="$(opensandbox_task_image_ref)"; then
    echo "[ERROR] failed to read OpenSandbox image from task.toml" >&2
    return 1
  fi
  if [[ "$task_image_ref" =~ [[:space:]] ]]; then
    echo "[ERROR] task docker_image must be a single image reference" >&2
    return 1
  fi
  if [[ -n "$task_image_ref" ]]; then
    HARBOR_OPENSANDBOX_IMAGE_REF="$task_image_ref"
    export HARBOR_OPENSANDBOX_IMAGE_REF
  fi
}

ensure_environment_backend() {
  validate_environment_backend
  if [[ "$HARBOR_ENVIRONMENT_TYPE" == "docker" ]]; then
    ensure_docker_daemon
    docker_hub_preflight_check
    return 0
  fi
  if [[ "$HARBOR_ENVIRONMENT_TYPE" == "e2b" ]]; then
    if [[ -z "${E2B_API_KEY:-}" ]]; then
      echo "[ERROR] E2B_API_KEY is required when HARBOR_ENVIRONMENT_TYPE=e2b" >&2
      exit 1
    fi
    echo "[INFO] using E2B environment; skip host Docker daemon and Docker Hub preflight"
  fi
  if [[ "$HARBOR_ENVIRONMENT_TYPE" == "qz" ]]; then
    echo "[INFO] using qz sandbox environment; skip host Docker daemon and Docker Hub preflight"
  fi
}

prepare_opensandbox_image_ref() {
  local automatic_bundle_manifest="$1"
  if [[ "$HARBOR_ENVIRONMENT_TYPE" != "opensandbox" ]]; then
    return 0
  fi
  if [[ -n "$HARBOR_OPENSANDBOX_BUNDLE_MANIFEST" ]]; then
    if [[ ! -f "$HARBOR_OPENSANDBOX_BUNDLE_MANIFEST" ]]; then
      echo "[ERROR] OpenSandbox Bundle Manifest not found: $HARBOR_OPENSANDBOX_BUNDLE_MANIFEST" >&2
      exit 1
    fi
    if [[ -z "$HARBOR_OPENSANDBOX_IMAGE_REF" ]]; then
      local bundle_python="${HARBOR_OPIK_PYTHON:-}"
      if [[ -z "$bundle_python" || ! -x "$bundle_python" ]]; then
        bundle_python="$(command -v python3)"
      fi
      if ! HARBOR_OPENSANDBOX_IMAGE_REF="$("$bundle_python" -c '
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
bundle = json.loads(path.read_text(encoding="utf-8"))
main = bundle.get("main") if bundle.get("schema_version") == 2 else bundle.get("main_service")
services = bundle.get("services") or {}
image = (services.get(main) or {}).get("image") or {}
ref = image.get("digest_ref") if bundle.get("schema_version") == 2 else image.get("sandbox_ref")
if not isinstance(main, str) or not isinstance(ref, str) or not ref:
    raise SystemExit("Bundle Manifest has no valid main image sandbox_ref")
print(ref)
' "$HARBOR_OPENSANDBOX_BUNDLE_MANIFEST")"; then
        echo "[ERROR] failed to resolve main image from OpenSandbox Bundle Manifest" >&2
        exit 1
      fi
    fi
    export HARBOR_OPENSANDBOX_IMAGE_REF HARBOR_OPENSANDBOX_BUNDLE_MANIFEST
    echo "[INFO] using OpenSandbox Bundle Manifest: $HARBOR_OPENSANDBOX_BUNDLE_MANIFEST" >&2
    return 0
  fi
  if [[ -n "$HARBOR_OPENSANDBOX_IMAGE_REF" ]]; then
    # Legacy explicit single-image mode remains supported until the runtime
    # environment consumes Bundle Manifests exclusively.
    return 0
  fi
  resolve_opensandbox_task_image_ref || return 1
  if [[ -n "$HARBOR_OPENSANDBOX_IMAGE_REF" ]]; then
    echo "[INFO] using task prebuilt OpenSandbox image: $HARBOR_OPENSANDBOX_IMAGE_REF" >&2
    return 0
  fi
  # DATASET_NAME can have a Harbor Registry alias (for example, seta ->
  # seta-env) while rollout workers still provide a real local DATASET_PATH.
  # OpenSandbox image preparation needs the local task definition, so decide
  # from the path itself instead of the dataset's registry capability.
  if [[ -z "${DATASET_PATH:-}" || ! -d "$DATASET_PATH" ]]; then
    echo "[ERROR] automatic OpenSandbox image preparation currently requires a local dataset path" >&2
    exit 1
  fi

  local manager_python="${HARBOR_OPIK_PYTHON:-}"
  if [[ -z "$manager_python" || ! -x "$manager_python" ]]; then
    manager_python="$(command -v python3)"
  fi
  local -a manager_cmd=(
    "$manager_python" "$HARBOR_OPENSANDBOX_IMAGE_MANAGER"
    --dataset-root "$DATASET_PATH"
    --include "$INCLUDE_TASKS"
    --registry "$YICLOUD_HARBOR_HOST"
    --project "$YICLOUD_HARBOR_PROJECT"
    --benchmark-name "$HARBOR_OPENSANDBOX_BENCHMARK"
    --docker-config "$HARBOR_OPENSANDBOX_DOCKER_CONFIG"
    --cache-root "$HARBOR_OPENSANDBOX_IMAGE_CACHE_ROOT"
    --platform "$HARBOR_OPENSANDBOX_IMAGE_PLATFORM"
    --tag-prefix "$HARBOR_OPENSANDBOX_IMAGE_TAG_PREFIX"
    --dockerhub-mirror-prefix "$HARBOR_OPENSANDBOX_DOCKERHUB_MIRROR_PREFIX"
    --apt-mirror "$HARBOR_OPENSANDBOX_APT_MIRROR"
    --build-args-json "$HARBOR_OPENSANDBOX_BUILD_ARGS_JSON"
    --build-network "$HARBOR_OPENSANDBOX_BUILD_NETWORK"
    --bundle-manifest-output "$automatic_bundle_manifest"
  )
  if [[ "$YICLOUD_HARBOR_TLS_VERIFY" == "1" ]]; then
    manager_cmd+=( --registry-tls-verify )
  fi
  if [[ "${HARBOR_FORCE_BUILD:-0}" == "1" || "${HARBOR_FORCE_BUILD:-0}" == "true" ]]; then
    manager_cmd+=( --force )
  fi
  if [[ "$HARBOR_OPENSANDBOX_BUILD_USE_PROXY" == "1" ]]; then
    manager_cmd+=( --use-proxy )
  fi
  if [[ "$HARBOR_DRY_RUN" == "1" ]]; then
    manager_cmd+=( --dry-run )
  else
    ensure_docker_daemon
  fi

  echo "[INFO] preparing OpenSandbox image for task: $INCLUDE_TASKS" >&2
  if ! HARBOR_OPENSANDBOX_IMAGE_REF="$("${manager_cmd[@]}")"; then
    echo "[ERROR] OpenSandbox image preparation failed" >&2
    exit 1
  fi
  if [[ -z "$HARBOR_OPENSANDBOX_IMAGE_REF" ]]; then
    echo "[ERROR] OpenSandbox image manager returned an empty image reference" >&2
    exit 1
  fi
  HARBOR_OPENSANDBOX_BUNDLE_MANIFEST="$automatic_bundle_manifest"
  if [[ ! -f "$HARBOR_OPENSANDBOX_BUNDLE_MANIFEST" ]]; then
    echo "[ERROR] OpenSandbox image manager did not write Bundle Manifest: $HARBOR_OPENSANDBOX_BUNDLE_MANIFEST" >&2
    exit 1
  fi
  export HARBOR_OPENSANDBOX_IMAGE_REF HARBOR_OPENSANDBOX_BUNDLE_MANIFEST
  echo "[INFO] OpenSandbox image ready: $HARBOR_OPENSANDBOX_IMAGE_REF" >&2
  echo "[INFO] OpenSandbox Bundle Manifest ready: $HARBOR_OPENSANDBOX_BUNDLE_MANIFEST" >&2
}

append_environment_backend_args() {
  cmd+=( --env "$HARBOR_ENVIRONMENT_SPEC" )
  if [[ "$HARBOR_ENVIRONMENT_TYPE" == "opensandbox" ]]; then
    cmd+=(
      --ek "image_ref=$HARBOR_OPENSANDBOX_IMAGE_REF"
      --ek "bundle_manifest_path=$HARBOR_OPENSANDBOX_BUNDLE_MANIFEST"
      --ek "lifecycle_minutes=$YICLOUD_SANDBOX_LIFECYCLE_MINUTES"
    )
  else
    # qz needs no extra args: the adapter reads its connection and template
    # settings from the exported qz environment variables, and the compose
    # overlay helper self-guards on the Docker backend.
    append_harbor_unprivileged_docker_compose
  fi
}

ensure_trace_plugin_source_if_needed() {
  if [[ "$HARBOR_DRY_RUN" == "1" ]]; then
    return 0
  fi
  local -a required=()
  if [[ "$AGENT" == "opencode" ]]; then
    if harbor_trace_to_opik_enabled; then
      required=("$TRACE_PLUGIN_OPENCODE_PLUGIN_SOURCE" "$TRACE_PLUGIN_OPENCODE_HOOK_SOURCE")
    fi
  elif harbor_agent_is_claude_code && harbor_trace_to_opik_enabled &&
    harbor_environment_supports_claude_hook_delivery &&
    [[ "$HARBOR_CC_OPIK_ENABLE_HOOK" == "1" && ! -f "$HARBOR_CC_HOOK_SOURCE" ]]; then
    # With tracing off the realtime hook is forced off at command
    # construction. Docker bind-mounts the hook, while OpenSandbox
    # materializes the same read-only mount after the Sandbox starts.
    # Claude tracing is best-effort: a missing hook must not prevent the
    # benchmark task, verifier, or cleanup from running.
    echo "[WARN] CC hook source not found, disable realtime hook: $HARBOR_CC_HOOK_SOURCE" >&2
    echo "[WARN] initialize the plugin submodule or set HARBOR_CC_HOOK_SOURCE to an existing file" >&2
    HARBOR_CC_OPIK_ENABLE_HOOK=0
    export HARBOR_CC_OPIK_ENABLE_HOOK
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
  local uv_bin uvx_bin curl_shim
  if [[ -z "$target_dir" ]]; then
    return 1
  fi
  uv_bin="$(command -v uv || true)"
  uvx_bin="$(command -v uvx || true)"
  curl_shim="$SCRIPT_DIR/verifier-tools/curl"
  if [[ -z "$uv_bin" || -z "$uvx_bin" ]]; then
    echo "[WARN] uv/uvx not found on host; verifier will use its normal uv install path" >&2
    return 1
  fi
  if [[ ! -x "$curl_shim" ]]; then
    echo "[WARN] verifier offline curl shim is missing or not executable: $curl_shim" >&2
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
  cp -f "$curl_shim" "$target_dir/curl"
  chmod +x "$target_dir/uv" "$target_dir/uvx" "$target_dir/curl"
  # The curl shim makes benchmark boilerplate such as
  # `curl https://astral.sh/uv/.../install.sh | sh` install these local
  # binaries without touching the network. The common installer locations
  # remain ahead of this directory so the task observes its expected layout.
  cat >"$target_dir/env" <<EOF
export PATH="\$HOME/.local/bin:$HARBOR_VERIFIER_UV_BIN_DIR_MOUNT_PATH:\$PATH"
EOF
}

verifier_uv_bin_ready() {
  [[ -n "$VERIFIER_UV_BIN_DIR_SOURCE" ]] \
    && [[ -x "$VERIFIER_UV_BIN_DIR_SOURCE/uv" ]] \
    && [[ -x "$VERIFIER_UV_BIN_DIR_SOURCE/uvx" ]] \
    && [[ -x "$VERIFIER_UV_BIN_DIR_SOURCE/curl" ]]
}

opensandbox_verifier_tool_env_required() {
  [[ "$HARBOR_ENVIRONMENT_TYPE" == "opensandbox" ]] \
    && { verifier_uv_bin_ready || verifier_runtime_bundle_required; }
}

append_opensandbox_verifier_tool_env() {
  local verifier_path_prefix=""
  if verifier_uv_bin_ready; then
    verifier_path_prefix="/root/.local/bin:/home/oai/.local/bin:/home/agent/.local/bin:/home/ubuntu/.local/bin"
    if [[ -n "${HARBOR_VERIFIER_UV_HOME:-}" ]]; then
      verifier_path_prefix="$HARBOR_VERIFIER_UV_HOME/.local/bin:$verifier_path_prefix"
    fi
    verifier_path_prefix="$verifier_path_prefix:$HARBOR_VERIFIER_UV_BIN_DIR_MOUNT_PATH"
    cmd+=(
      --ve "HARBOR_VERIFIER_UV_BIN_DIR=$HARBOR_VERIFIER_UV_BIN_DIR_MOUNT_PATH"
    )
  fi

  if verifier_runtime_bundle_required; then
    validate_verifier_runtime_bundle_transport || return 1
    if ! verifier_runtime_bundle_ready; then
      echo "[ERROR] verifier runtime bundle $VERIFIER_RUNTIME_BUNDLE_ID selected for $HARBOR_OPENSANDBOX_BENCHMARK, but its archive is missing or invalid" >&2
      return 1
    fi
    if [[ -n "$verifier_path_prefix" ]]; then
      verifier_path_prefix="$VERIFIER_RUNTIME_BUNDLE_ROOT/bin:$verifier_path_prefix"
    else
      verifier_path_prefix="$VERIFIER_RUNTIME_BUNDLE_ROOT/bin"
    fi
    cmd+=(
      --ve "HARBOR_VERIFIER_RUNTIME_BUNDLE_ID=$VERIFIER_RUNTIME_BUNDLE_ID"
      --ve "HARBOR_VERIFIER_RUNTIME_BUNDLE_ARCHIVE=$VERIFIER_RUNTIME_BUNDLE_ARCHIVE_MOUNT_PATH"
      --ve "HARBOR_VERIFIER_RUNTIME_BUNDLE_ROOT=$VERIFIER_RUNTIME_BUNDLE_ROOT"
    )
    echo "[INFO] OpenSandbox verifier runtime bundle: benchmark=$HARBOR_OPENSANDBOX_BENCHMARK bundle=$VERIFIER_RUNTIME_BUNDLE_ID" >&2
  fi
  if [[ -n "$verifier_path_prefix" ]]; then
    cmd+=(
      --ve "HARBOR_VERIFIER_PATH_PREPEND=$verifier_path_prefix"
    )
  fi
}

configure_e2b_verifier_uv_upload() {
  HARBOR_E2B_VERIFIER_UV_SOURCE=""
  if [[ "$HARBOR_ENVIRONMENT_TYPE" == "e2b" || "$HARBOR_ENVIRONMENT_TYPE" == "qz" ]] \
    && verifier_uv_bin_ready; then
    HARBOR_E2B_VERIFIER_UV_SOURCE="$VERIFIER_UV_BIN_DIR_SOURCE"
    if [[ "$HARBOR_ENVIRONMENT_TYPE" == "e2b" ]]; then
      echo "[INFO] E2B verifier uv tools will be uploaded after sandbox start"
    else
      echo "[INFO] qz verifier uv tools will be uploaded after sandbox start"
    fi
  fi
  export HARBOR_E2B_VERIFIER_UV_SOURCE
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

prepare_local_dataset_if_needed() {
  if harbor_uses_registry_dataset; then
    return 0
  fi

  harbor_ensure_dataset
  harbor_prepare_task_file
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
  if [[ "$HARBOR_ENVIRONMENT_TYPE" != "docker" ]]; then
    return 0
  fi
  if [[ "$HARBOR_SKIP_DOCKERHUB_PREFLIGHT" == "1" ]]; then
    echo "[INFO] HARBOR_SKIP_DOCKERHUB_PREFLIGHT=1, skip Docker Hub connectivity preflight"
    return 0
  fi

  if task_is_included "fix-git" && local_image_exists "$FIX_GIT_IMAGE_NAME"; then
    maybe_report_fix_git_image
    echo "[INFO] local fix-git image present, skip Docker Hub connectivity preflight"
    return 0
  fi

  local timeout="$HARBOR_DOCKERHUB_CHECK_TIMEOUT"
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
    if [[ "$HARBOR_DOCKERHUB_PREFLIGHT_STRICT" == "1" ]]; then
      online_env_event "preflight" "docker_registry" "connectivity_unavailable" "critical" "true" "Docker Hub preflight failed in strict mode"
      echo "[ERROR] Docker Hub preflight failed in strict mode." >&2
      echo "[ERROR] fix network/proxy/registry mirror first, or set HARBOR_DOCKERHUB_PREFLIGHT_STRICT=0 / HARBOR_SKIP_DOCKERHUB_PREFLIGHT=1." >&2
      exit 1
    fi
    online_env_event "preflight" "docker_registry" "connectivity_degraded" "warning" "false" "Docker Hub preflight failed; continuing because strict mode is disabled"
    echo "[WARN] Docker Hub preflight failed, continuing (strict mode disabled)." >&2
    echo "[WARN] if image pull fails later, set a proxy/registry mirror and retry." >&2
  fi
}

normalize_json_or_fail() {
  local raw="$1"
  python3 "$SCRIPT_DIR/harbor_shell_utils.py" normalize-json "$raw"
}

apply_min_test_defaults() {
  if [[ "$MIN_TEST" != "1" ]]; then
    return 0
  fi

  if [[ "$HARBOR_RUNS" == "10" ]]; then
    HARBOR_RUNS="1"
  fi
  if [[ -z "$HARBOR_LIMIT" ]]; then
    HARBOR_LIMIT="1"
  fi
  if [[ -z "$INCLUDE_TASKS" && -n "$MIN_TEST_INCLUDE_TASK" ]]; then
    INCLUDE_TASKS="$MIN_TEST_INCLUDE_TASK"
  fi

  echo "[INFO] MIN_TEST=1 enabled (runs=$HARBOR_RUNS, limit=$HARBOR_LIMIT, include_tasks=$INCLUDE_TASKS)"
}

run_oracle_task() {
  local effective_jobs_root="$JOBS_ROOT"
  if ! mkdir -p "$effective_jobs_root" 2>/dev/null; then
    effective_jobs_root="$HOME/harbor_jobs"
    mkdir -p "$effective_jobs_root"
    echo "[WARN] unable to use JOBS_ROOT=$JOBS_ROOT, fallback to $effective_jobs_root"
  fi

  local job_name out_dir
  job_name="$(date +%Y-%m-%d__%H-%M-%S)"
  out_dir="$effective_jobs_root/$job_name"
  if harbor_is_fixer_verification_main; then
    printf '%s\n' "$out_dir" > "$HARBOR_JOB_DIR_FILE"
  fi
  mkdir -p "$out_dir"

  VERIFIER_UV_BIN_DIR_SOURCE="$(mktemp -d "${RUNTIME_DIR%/}/verifier-uv.oracle.XXXXXX" 2>/dev/null || true)"
  if [[ -n "$VERIFIER_UV_BIN_DIR_SOURCE" ]]; then
    prepare_verifier_uv_bin "$VERIFIER_UV_BIN_DIR_SOURCE" || true
  else
    echo "[WARN] failed to create verifier uv backup dir; verifier will use its normal uv install path" >&2
  fi
  configure_e2b_verifier_uv_upload

  if [[ "$HARBOR_DRY_RUN" != "1" ]]; then
    harbor_validate_runner_cli
  fi
  prepare_opensandbox_image_ref "$out_dir/opensandbox-bundle.json" || return 1

  local cmd=(
    "$HARBOR_CLI_BIN" run
    -y
    --n-concurrent "$HARBOR_N_CONCURRENT"
    --max-retries "$HARBOR_MAX_RETRIES"
    -o "$out_dir"
    -k "$HARBOR_RUNS"
    -a oracle
    --timeout-multiplier "$HARBOR_TIMEOUT_MULTIPLIER"
    --agent-setup-timeout-multiplier "$HARBOR_AGENT_SETUP_TIMEOUT_MULTIPLIER"
  )
  if harbor_uses_local_opensandbox_dataset; then
    cmd+=( --path "$DATASET_PATH" )
  elif harbor_uses_registry_dataset; then
    cmd+=( --dataset "$(harbor_registry_dataset_name)" )
  else
    cmd+=( --path "$DATASET_PATH" )
  fi
  append_environment_backend_args
  if opensandbox_verifier_tool_env_required; then
    local verifier_mounts_json
    local -a verifier_mount_args=()
    if verifier_uv_bin_ready; then
      verifier_mount_args+=(
        --mount "$VERIFIER_UV_BIN_DIR_SOURCE"
        "$HARBOR_VERIFIER_UV_BIN_DIR_MOUNT_PATH" always
      )
    fi
    if verifier_runtime_bundle_required; then
      if ! verifier_runtime_bundle_ready; then
        echo "[ERROR] verifier runtime bundle $VERIFIER_RUNTIME_BUNDLE_ID selected for $HARBOR_OPENSANDBOX_BENCHMARK, but its archive is missing" >&2
        return 1
      fi
      verifier_mount_args+=(
        --mount "$VERIFIER_RUNTIME_BUNDLE_ARCHIVE_SOURCE"
        "$VERIFIER_RUNTIME_BUNDLE_ARCHIVE_MOUNT_PATH" always
      )
    fi
    verifier_mounts_json="$(
      python3 "$SCRIPT_DIR/harbor_shell_utils.py" readonly-mounts \
        "${verifier_mount_args[@]}"
    )"
    cmd+=(
      --mounts-json "$verifier_mounts_json"
    )
    append_opensandbox_verifier_tool_env
  elif [[ "$HARBOR_ENVIRONMENT_TYPE" != "e2b" && "$HARBOR_ENVIRONMENT_TYPE" != "qz" ]] \
    && verifier_uv_bin_ready; then
    local verifier_mounts_json verifier_uv_path_prefix
    verifier_mounts_json="$(
      python3 "$SCRIPT_DIR/harbor_shell_utils.py" readonly-mounts \
        --mount "$VERIFIER_UV_BIN_DIR_SOURCE" \
        "$HARBOR_VERIFIER_UV_BIN_DIR_MOUNT_PATH" always
    )"
    verifier_uv_path_prefix="/root/.local/bin:/home/oai/.local/bin:/home/agent/.local/bin:/home/ubuntu/.local/bin"
    if [[ -n "${HARBOR_VERIFIER_UV_HOME:-}" ]]; then
      verifier_uv_path_prefix="$HARBOR_VERIFIER_UV_HOME/.local/bin:$verifier_uv_path_prefix"
    fi
    cmd+=(
      --mounts-json "$verifier_mounts_json"
      --ve "PATH=$verifier_uv_path_prefix:$HARBOR_VERIFIER_UV_BIN_DIR_MOUNT_PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
      --ve "HARBOR_VERIFIER_UV_BIN_DIR=$HARBOR_VERIFIER_UV_BIN_DIR_MOUNT_PATH"
    )
  fi
  if [[ "$HARBOR_ENVIRONMENT_TYPE" == "e2b" || "$HARBOR_ENVIRONMENT_TYPE" == "qz" ]] \
    && verifier_uv_bin_ready; then
    local verifier_uv_path_prefix
    verifier_uv_path_prefix="/root/.local/bin:/home/oai/.local/bin:/home/agent/.local/bin:/home/ubuntu/.local/bin"
    if [[ -n "${HARBOR_VERIFIER_UV_HOME:-}" ]]; then
      verifier_uv_path_prefix="$HARBOR_VERIFIER_UV_HOME/.local/bin:$verifier_uv_path_prefix"
    fi
    cmd+=(
      --ve "PATH=$verifier_uv_path_prefix:$HARBOR_VERIFIER_UV_BIN_DIR_MOUNT_PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
      --ve "HARBOR_VERIFIER_UV_BIN_DIR=$HARBOR_VERIFIER_UV_BIN_DIR_MOUNT_PATH"
    )
  fi
  if [[ -n "${PIP_INDEX_URL:-}" ]]; then
    cmd+=( --ve "PIP_INDEX_URL=$PIP_INDEX_URL" )
  fi
  if [[ -n "${PIP_EXTRA_INDEX_URL:-}" ]]; then
    cmd+=( --ve "PIP_EXTRA_INDEX_URL=$PIP_EXTRA_INDEX_URL" )
  fi
  if [[ -n "${PIP_TRUSTED_HOST:-}" ]]; then
    cmd+=( --ve "PIP_TRUSTED_HOST=$PIP_TRUSTED_HOST" )
  fi
  if [[ -n "${UV_INDEX_URL:-}" ]]; then
    cmd+=( --ve "UV_INDEX_URL=$UV_INDEX_URL" )
  fi
  if [[ -n "${UV_DEFAULT_INDEX:-}" ]]; then
    cmd+=( --ve "UV_DEFAULT_INDEX=$UV_DEFAULT_INDEX" )
  fi
  if [[ -n "${UV_PYTHON_DOWNLOADS:-}" ]]; then
    cmd+=( --ve "UV_PYTHON_DOWNLOADS=$UV_PYTHON_DOWNLOADS" )
  fi
  if [[ -n "${UV_PYTHON_PREFERENCE:-}" ]]; then
    cmd+=( --ve "UV_PYTHON_PREFERENCE=$UV_PYTHON_PREFERENCE" )
  fi
  if [[ -n "$HARBOR_LIMIT" ]]; then
    cmd+=( -l "$HARBOR_LIMIT" )
  fi
  if [[ -n "$INCLUDE_TASKS" ]]; then
    local task_name
    local -a include_arr
    IFS=',' read -r -a include_arr <<< "$INCLUDE_TASKS"
    for task_name in "${include_arr[@]}"; do
      task_name="${task_name#"${task_name%%[![:space:]]*}"}"
      task_name="${task_name%"${task_name##*[![:space:]]}"}"
      if [[ -n "$task_name" ]]; then
        cmd+=( -i "$(harbor_registry_task_name "$task_name")" )
      fi
    done
  fi
  if [[ "$HARBOR_DEBUG" == "1" ]]; then
    cmd+=( --debug )
  fi
  if [[ "$HARBOR_FORCE_BUILD" == "1" || "$HARBOR_FORCE_BUILD" == "true" ]]; then
    cmd+=( --force-build )
  fi

  echo "[INFO] running Harbor Oracle"
  echo "[INFO] environment: $HARBOR_ENVIRONMENT_TYPE ($HARBOR_ENVIRONMENT_SPEC)"
  echo "[INFO] output dir: $out_dir"
  echo "[INFO] n_concurrent: $HARBOR_N_CONCURRENT | max_retries: $HARBOR_MAX_RETRIES"

  if [[ "$HARBOR_DRY_RUN" == "1" ]]; then
    echo "[INFO] HARBOR_DRY_RUN=1, skip execution"
    printf '[INFO] command:'
    printf ' %q' "${cmd[@]}"
    printf '\n'
    return 0
  fi

  # sitecustomize.py activates the shared E2B runtime patches before Harbor
  # imports either the built-in or prebuilt environment. Oracle needs this
  # path too even though it does not instantiate the Claude Code agent.
  export PYTHONPATH="$HARBOR_CLAUDE_CODE_DIR:$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"
  "${cmd[@]}"
  echo "[INFO] Oracle completed"
  echo "[INFO] results: $out_dir"
}

run_harbor() {
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
  if harbor_publishes_job_dir; then
    printf '%s\n' "$out_dir" > "$HARBOR_JOB_DIR_FILE"
  fi
  mkdir -p "$out_dir"

  if [[ "$HARBOR_DRY_RUN" == "1" ]]; then
    echo "[INFO] HARBOR_DRY_RUN=1, skip Opik project preflight check"
  elif ! harbor_trace_to_opik_enabled; then
    echo "[INFO] Opik tracing disabled, skip project preflight check"
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
  if ! normalized_llm_kwargs="$(normalize_json_or_fail "$HARBOR_LLM_KWARGS")"; then
    online_env_event "agent_setup" "agent_configuration" "invalid_llm_kwargs" "critical" "true" "HARBOR_LLM_KWARGS is not valid JSON"
    echo "[ERROR] HARBOR_LLM_KWARGS is not valid JSON" >&2
    echo "[ERROR] current HARBOR_LLM_KWARGS: $HARBOR_LLM_KWARGS" >&2
    exit 1
  fi
  if [[ "$HARBOR_ENVIRONMENT_TYPE" == "docker" || "$HARBOR_ENVIRONMENT_TYPE" == "e2b" \
    || "$HARBOR_ENVIRONMENT_TYPE" == "opensandbox" || "$HARBOR_ENVIRONMENT_TYPE" == "qz" ]]; then
    VERIFIER_UV_BIN_DIR_SOURCE="$(mktemp -d "${RUNTIME_DIR%/}/verifier-uv.${job_name}.XXXXXX" 2>/dev/null || true)"
    if [[ -n "$VERIFIER_UV_BIN_DIR_SOURCE" ]]; then
      prepare_verifier_uv_bin "$VERIFIER_UV_BIN_DIR_SOURCE" || true
    else
      echo "[WARN] failed to create verifier uv backup dir; verifier will use its normal uv install path" >&2
    fi
  fi
  configure_e2b_verifier_uv_upload

  local effective_harbor_task_id include_task
  effective_harbor_task_id="${HARBOR_TASK_ID:-}"
  if [[ -z "$effective_harbor_task_id" ]]; then
    include_task="$(single_include_task "${INCLUDE_TASKS:-${HARBOR_INCLUDE_TASKS:-}}" || true)"
    if [[ -n "$include_task" ]]; then
      effective_harbor_task_id="$include_task"
    fi
  fi

  if [[ -z "$HARBOR_ANTHROPIC_AUTH_TOKEN" ]]; then
    local inferred_api_key
    inferred_api_key="$(
      python3 "$SCRIPT_DIR/harbor_shell_utils.py" json-string-field \
        "$normalized_llm_kwargs" api_key
    )"
    if [[ -n "$inferred_api_key" ]]; then
      HARBOR_ANTHROPIC_AUTH_TOKEN="$inferred_api_key"
      echo "[INFO] HARBOR_ANTHROPIC_AUTH_TOKEN is empty; using api_key from HARBOR_LLM_KWARGS"
    else
      online_env_event "agent_setup" "agent_configuration" "auth_token_missing" "critical" "true" "HARBOR_ANTHROPIC_AUTH_TOKEN and HARBOR_LLM_KWARGS.api_key are both missing"
      echo "[ERROR] HARBOR_ANTHROPIC_AUTH_TOKEN is empty and HARBOR_LLM_KWARGS.api_key is missing" >&2
      exit 1
    fi
  fi

  if [[ "$HARBOR_DRY_RUN" != "1" ]] && ! harbor_runner_cli_ready; then
    harbor_validate_runner_cli
  fi

  local cmd=(
    "$HARBOR_OPIK_BIN" harbor run
    -y
    --n-concurrent "$HARBOR_N_CONCURRENT"
    --max-retries "$HARBOR_MAX_RETRIES"
    -o "$out_dir"
    -k "$HARBOR_RUNS"
    --ae "HARBOR_LOCAL_WHEEL_SERVER_URL=${HARBOR_LOCAL_WHEEL_SERVER_URL:-}"
    --ae "PIP_DEFAULT_TIMEOUT=$HARBOR_PIP_DEFAULT_TIMEOUT"
    --ae "PIP_RETRIES=$HARBOR_PIP_RETRIES"
    --ae "PIP_DISABLE_PIP_VERSION_CHECK=1"
    --ae "HARBOR_DATASET=$(harbor_metadata_dataset_name)"
    --ae "HARBOR_RUN_ID=${HARBOR_RUN_ID:-$job_name}"
    --ae "HARBOR_TASK_ID=$effective_harbor_task_id"
    --ae "HARBOR_INCLUDE_TASKS=${HARBOR_INCLUDE_TASKS:-$INCLUDE_TASKS}"
    --ae "INCLUDE_TASKS=$INCLUDE_TASKS"
    --timeout-multiplier "$HARBOR_TIMEOUT_MULTIPLIER"
    --agent-setup-timeout-multiplier "$HARBOR_AGENT_SETUP_TIMEOUT_MULTIPLIER"
  )
  if harbor_agent_is_pi; then
    # Pi's thinking level is rendered into PI_SETTINGS_CONFIG. It is not an
    # agent kwarg, and Pi setup consumes only the cached runtime archives.
    cmd+=(
      --ak "version=$PI_VERSION"
      --ae "AGENT_FLEET_API_KEY=$HARBOR_ANTHROPIC_AUTH_TOKEN"
      --ae "PI_OFFLINE=1"
      --ae "PI_CACHE_DIR=$HARBOR_CC_PY_WHEEL_DIR_MOUNT_PATH"
      --ae "PI_NODE_RUNTIME_PATH=$HARBOR_CC_PY_WHEEL_DIR_MOUNT_PATH/$PI_NODE_RUNTIME_BASENAME"
      --ae "PI_RUNTIME_TAR_PATH=$HARBOR_CC_PY_WHEEL_DIR_MOUNT_PATH/$PI_RUNTIME_BASENAME"
      --ae "PI_MODELS_CONFIG=$PI_MODELS_CONFIG"
      --ae "PI_SETTINGS_CONFIG=$PI_SETTINGS_CONFIG"
      --ae "PI_EXTENSION_DIR=$PI_EXTENSION_DIR"
    )
  else
    cmd+=(
      --ak "version=$CLAUDE_CODE_VERSION"
      --ak "disallowed_tools=$HARBOR_DISALLOWED_TOOLS"
      --ak "append_system_prompt=$HARBOR_APPEND_SYSTEM_PROMPT"
      --ak "api_base=$HARBOR_API_BASE"
      --ak "llm_kwargs=$normalized_llm_kwargs"
      --ak "max_new_tokens=$HARBOR_MAX_NEW_TOKENS"
      --ak "model_info=$HARBOR_MODEL_INFO"
      --ae "ANTHROPIC_BASE_URL=$HARBOR_ANTHROPIC_BASE_URL"
      --ae "ANTHROPIC_AUTH_TOKEN=$HARBOR_ANTHROPIC_AUTH_TOKEN"
      --ae "ANTHROPIC_CUSTOM_HEADERS=$HARBOR_ANTHROPIC_CUSTOM_HEADERS"
      --ae "ANTHROPIC_MODEL=$HARBOR_ANTHROPIC_MODEL"
      --ae "ANTHROPIC_DEFAULT_OPUS_MODEL=$HARBOR_ANTHROPIC_DEFAULT_OPUS_MODEL"
      --ae "ANTHROPIC_DEFAULT_SONNET_MODEL=$HARBOR_ANTHROPIC_DEFAULT_SONNET_MODEL"
      --ae "ANTHROPIC_DEFAULT_HAIKU_MODEL=$HARBOR_ANTHROPIC_DEFAULT_HAIKU_MODEL"
      --ae "CLAUDE_CODE_SUBAGENT_MODEL=$HARBOR_CLAUDE_CODE_SUBAGENT_MODEL"
      --ae "CLAUDE_CODE_EFFORT_LEVEL=$HARBOR_CLAUDE_CODE_EFFORT_LEVEL"
      --ae "CLAUDE_CODE_MAX_OUTPUT_TOKENS=$HARBOR_CLAUDE_CODE_MAX_OUTPUT_TOKENS"
      --ae "CLAUDE_CODE_DISABLE_AUTOUPDATER=$HARBOR_CLAUDE_CODE_DISABLE_AUTOUPDATER"
      --ae "CC_OPIK_DEBUG=$HARBOR_CC_OPIK_DEBUG"
      --ae "CC_OPIK_INSTALL_DEPS=$HARBOR_CC_OPIK_INSTALL_DEPS"
      --ae "CC_OPIK_HOOK_MOUNT_PATH=$HARBOR_CC_HOOK_MOUNT_PATH"
      --ae "CC_OPIK_CLAUDE_TGZ_PATH=$HARBOR_CC_CLAUDE_TGZ_MOUNT_PATH"
      --ae "CC_OPIK_PY_WHEEL_DIR=$HARBOR_CC_PY_WHEEL_DIR_MOUNT_PATH"
      --ae "CC_OPIK_NPM_CACHE_DIR=$HARBOR_CC_NPM_CACHE_MOUNT_PATH"
      --ae "HARBOR_LOCAL_CLAUDE_TGZ_URL=${HARBOR_LOCAL_CLAUDE_TGZ_URL:-}"
    )
    if [[ -n "$HARBOR_CC_WEB_MCP_SOURCE" ]]; then
      if [[ ! -f "$HARBOR_CC_WEB_MCP_SOURCE" ]]; then
        echo "[ERROR] Claude web MCP source not found: $HARBOR_CC_WEB_MCP_SOURCE" >&2
        return 1
      fi
      if [[ -z "${EXA_API_KEY:-}" ]]; then
        echo "[ERROR] EXA_API_KEY is required when HARBOR_CC_WEB_MCP_SOURCE is set" >&2
        return 1
      fi
      cmd+=(
        --ae "CC_WEB_MCP_PATH=$HARBOR_CC_WEB_MCP_MOUNT_PATH"
        --ae "EXA_API_KEY=$EXA_API_KEY"
      )
    fi
  fi
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
  if harbor_uses_local_opensandbox_dataset; then
    cmd+=( --path "$DATASET_PATH" )
  elif harbor_uses_registry_dataset; then
    cmd+=( --dataset "$(harbor_registry_dataset_name)" )
  else
    cmd+=( --path "$DATASET_PATH" )
  fi
  prepare_opensandbox_image_ref "$out_dir/opensandbox-bundle.json" || return 1
  append_environment_backend_args
  if [[ -n "${HARBOR_VERIFIER_UV_HOME:-}" ]]; then
    cmd+=( --ve "HOME=$HARBOR_VERIFIER_UV_HOME" )
  fi

  if [[ -n "${HARBOR_AGENT_TIMEOUT_MULTIPLIER:-}" ]]; then
    cmd+=( --agent-timeout-multiplier "$HARBOR_AGENT_TIMEOUT_MULTIPLIER" )
  fi

  if harbor_agent_is_claude_code; then
    if [[ -n "$HARBOR_AK_MAX_TURNS" ]]; then
      cmd+=( --ak "max_turns=$HARBOR_AK_MAX_TURNS" )
    fi
    if [[ -n "$HARBOR_AK_COLLECT_ROLLOUT_DETAILS" ]]; then
      cmd+=( --ak "collect_rollout_details=$HARBOR_AK_COLLECT_ROLLOUT_DETAILS" )
    fi
    if [[ -n "$HARBOR_AK_ENABLE_SUMMARIZE" ]]; then
      cmd+=( --ak "enable_summarize=$HARBOR_AK_ENABLE_SUMMARIZE" )
    fi
  fi

  local opik_host wheel_host model_host no_proxy_value
  opik_host="$(
    python3 "$SCRIPT_DIR/harbor_shell_utils.py" url-hostname "$OPIK_URL_OVERRIDE"
  )"
  wheel_host="$(
    python3 "$SCRIPT_DIR/harbor_shell_utils.py" url-hostname \
      "${HARBOR_LOCAL_WHEEL_SERVER_URL:-}"
  )"
  model_host="$(
    python3 "$SCRIPT_DIR/harbor_shell_utils.py" url-hostname \
      "$HARBOR_ANTHROPIC_BASE_URL"
  )"
  no_proxy_value="127.0.0.1,localhost,host.docker.internal"
  if [[ -n "$opik_host" ]]; then
    no_proxy_value="$no_proxy_value,$opik_host"
  fi
  if [[ -n "$wheel_host" ]]; then
    no_proxy_value="$no_proxy_value,$wheel_host"
  fi
  if harbor_agent_is_pi && [[ -n "$model_host" ]]; then
    # Pi sends its OpenAI-compatible request straight to the gateway.
    no_proxy_value="$no_proxy_value,$model_host"
  fi
  cmd+=( --ae "NO_PROXY=$no_proxy_value" --ae "no_proxy=$no_proxy_value" )

  local hook_mount_enabled=0
  # The hook has no Opik server to talk to when tracing is off, so an
  # exported HARBOR_CC_OPIK_ENABLE_HOOK=1 (e.g. persisted by setup.sh) must not
  # re-enable it.
  if harbor_agent_is_claude_code \
    && [[ "$HARBOR_CC_OPIK_ENABLE_HOOK" == "1" ]] \
    && harbor_environment_supports_claude_hook_delivery &&
    harbor_trace_to_opik_enabled; then
    if [[ -f "$HARBOR_CC_HOOK_SOURCE" ]]; then
      hook_mount_enabled=1
      cmd+=(
        --ae "CC_OPIK_ENABLE_HOOK=true"
        --ae "TRACE_TO_OPIK=true"
      )
    else
      echo "[WARN] CC hook source not found, disable realtime hook: $HARBOR_CC_HOOK_SOURCE" >&2
      echo "[WARN] initialize the plugin submodule or set HARBOR_CC_HOOK_SOURCE to an existing file" >&2
      cmd+=( --ae "CC_OPIK_ENABLE_HOOK=false" )
    fi
  else
    if [[ "$HARBOR_CC_OPIK_ENABLE_HOOK" == "1" && "$HARBOR_ENVIRONMENT_TYPE" == "e2b" ]]; then
      echo "[INFO] disable realtime Claude hook for E2B because its source is a host bind mount"
    fi
    cmd+=( --ae "CC_OPIK_ENABLE_HOOK=false" )
  fi

  local mounts_json="[]" agent_package_source="$HARBOR_CC_CLAUDE_TGZ_SOURCE"
  if harbor_agent_is_pi; then
    agent_package_source=""
  fi
  if [[ "$HARBOR_ENVIRONMENT_TYPE" == "docker" || "$HARBOR_ENVIRONMENT_TYPE" == "opensandbox" ]]; then
    local -a mount_args=()
    if [[ "$hook_mount_enabled" == "1" ]]; then
      mount_args+=( --mount "$HARBOR_CC_HOOK_SOURCE" "$HARBOR_CC_HOOK_MOUNT_PATH" always )
    fi
    if [[ -n "$agent_package_source" ]]; then
      mount_args+=( --mount "$agent_package_source" "$HARBOR_CC_CLAUDE_TGZ_MOUNT_PATH" exists )
    fi
    if [[ -n "$HARBOR_CC_PY_WHEEL_DIR_SOURCE" ]]; then
      mount_args+=( --mount "$HARBOR_CC_PY_WHEEL_DIR_SOURCE" "$HARBOR_CC_PY_WHEEL_DIR_MOUNT_PATH" exists )
    fi
    if harbor_agent_is_claude_code && [[ -n "$HARBOR_CC_WEB_MCP_SOURCE" ]]; then
      mount_args+=( --mount "$HARBOR_CC_WEB_MCP_SOURCE" "$HARBOR_CC_WEB_MCP_MOUNT_PATH" always )
    fi
    if [[ -n "$VERIFIER_UV_BIN_DIR_SOURCE" ]]; then
      mount_args+=( --mount "$VERIFIER_UV_BIN_DIR_SOURCE" "$HARBOR_VERIFIER_UV_BIN_DIR_MOUNT_PATH" uv-bin )
    fi
    if harbor_agent_is_pi && [[ -n "${PI_EXTENSION_SOURCE:-}" && -d "$PI_EXTENSION_SOURCE" ]]; then
      local pi_extension_file
      for pi_extension_file in "$PI_EXTENSION_SOURCE"/*.ts; do
        [[ -e "$pi_extension_file" ]] || continue
        mount_args+=( --mount "$PI_EXTENSION_SOURCE" "$PI_EXTENSION_DIR" always )
        break
      done
    fi
    mounts_json="$(
      python3 "$SCRIPT_DIR/harbor_shell_utils.py" readonly-mounts \
        "${mount_args[@]}"
    )"
  elif [[ "$HARBOR_ENVIRONMENT_TYPE" == "e2b" ]]; then
    echo "[INFO] E2B environment does not support host bind mounts; skip hook, dependency, and verifier uv mounts"
  fi
  if [[ "$mounts_json" != "[]" ]]; then
    cmd+=( --mounts-json "$mounts_json" )
  fi
  if opensandbox_verifier_tool_env_required; then
    append_opensandbox_verifier_tool_env
  elif [[ "$HARBOR_ENVIRONMENT_TYPE" == "docker" || "$HARBOR_ENVIRONMENT_TYPE" == "e2b" \
    || "$HARBOR_ENVIRONMENT_TYPE" == "qz" ]] && verifier_uv_bin_ready; then
    local verifier_uv_path_prefix
    verifier_uv_path_prefix="/root/.local/bin:/home/oai/.local/bin:/home/agent/.local/bin:/home/ubuntu/.local/bin"
    if [[ -n "${HARBOR_VERIFIER_UV_HOME:-}" ]]; then
      verifier_uv_path_prefix="$HARBOR_VERIFIER_UV_HOME/.local/bin:$verifier_uv_path_prefix"
    fi
    cmd+=(
      --ve "PATH=$verifier_uv_path_prefix:$HARBOR_VERIFIER_UV_BIN_DIR_MOUNT_PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
      --ve "HARBOR_VERIFIER_UV_BIN_DIR=$HARBOR_VERIFIER_UV_BIN_DIR_MOUNT_PATH"
    )
  fi

  append_package_environment_args

  if [[ "$HARBOR_DEBUG" == "1" ]]; then
    cmd+=( --debug )
  fi

  if [[ -n "$AGENT" ]] && ! harbor_agent_is_pi; then
    cmd+=( -a "$AGENT" )
  fi

  if [[ -n "$HARBOR_AGENT_IMPORT_PATH" ]]; then
    cmd+=( --agent-import-path "$HARBOR_AGENT_IMPORT_PATH" )
  fi

  if [[ -n "$HARBOR_MODEL" ]]; then
    cmd+=( -m "$HARBOR_MODEL" )
  fi

  if [[ -n "$HARBOR_LIMIT" ]]; then
    cmd+=( -l "$HARBOR_LIMIT" )
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

  if [[ -n "$HARBOR_RETRY_INCLUDE_EXCEPTIONS" ]]; then
    IFS=',' read -r -a retry_include_arr <<< "$HARBOR_RETRY_INCLUDE_EXCEPTIONS"
    for exception_name in "${retry_include_arr[@]}"; do
      exception_name="${exception_name#"${exception_name%%[![:space:]]*}"}"
      exception_name="${exception_name%"${exception_name##*[![:space:]]}"}"
      if [[ -n "$exception_name" ]]; then
        cmd+=( --retry-include "$exception_name" )
      fi
    done
  fi

  if [[ -n "$HARBOR_RETRY_EXCLUDE_EXCEPTIONS" ]]; then
    IFS=',' read -r -a retry_exclude_arr <<< "$HARBOR_RETRY_EXCLUDE_EXCEPTIONS"
    for exception_name in "${retry_exclude_arr[@]}"; do
      exception_name="${exception_name#"${exception_name%%[![:space:]]*}"}"
      exception_name="${exception_name%"${exception_name##*[![:space:]]}"}"
      if [[ -n "$exception_name" ]]; then
        cmd+=( --retry-exclude "$exception_name" )
      fi
    done
  fi

  if [[ "${HARBOR_FORCE_BUILD:-0}" == "1" || "${HARBOR_FORCE_BUILD:-0}" == "true" ]]; then
    # Some datasets publish prebuilt task images, but registry mirrors can return
    # 429/not-found. Force-build bypasses those prebuilt pulls when needed.
    cmd+=( --force-build )
  fi

  if harbor_trace_to_opik_enabled; then
    echo "[INFO] running Harbor with real-time Opik tracking"
    echo "[INFO] project: $OPIK_PROJECT_NAME"
  else
    echo "[INFO] running Harbor without Opik tracing"
  fi
  if harbor_uses_local_opensandbox_dataset; then
    echo "[INFO] agent: $AGENT | runs: $HARBOR_RUNS | path: $DATASET_PATH"
  elif harbor_uses_registry_dataset; then
    echo "[INFO] agent: $AGENT | runs: $HARBOR_RUNS | dataset: $(harbor_registry_dataset_name)"
  else
    echo "[INFO] agent: $AGENT | runs: $HARBOR_RUNS | path: $DATASET_PATH"
  fi
  echo "[INFO] agent_import_path: ${HARBOR_AGENT_IMPORT_PATH:-<none>}"
  echo "[INFO] output dir: $out_dir"
  if harbor_trace_to_opik_enabled; then
    echo "[INFO] dashboard: ${OPIK_BASE%/}/${OPIK_WORKSPACE}/home"
  fi
  echo "[INFO] model: $HARBOR_MODEL"
  echo "[INFO] environment: $HARBOR_ENVIRONMENT_TYPE"
  if harbor_agent_is_pi; then
    echo "[INFO] pi version: $PI_VERSION | thinking: $PI_THINKING_LEVEL"
  else
    echo "[INFO] claude max_turns: ${HARBOR_AK_MAX_TURNS:-<default>}"
  fi
  echo "[INFO] n_concurrent: $HARBOR_N_CONCURRENT | max_retries: $HARBOR_MAX_RETRIES"
  echo "[INFO] retry_include_exceptions: ${HARBOR_RETRY_INCLUDE_EXCEPTIONS:-<all-except-excludes>}"
  echo "[INFO] retry_exclude_exceptions: ${HARBOR_RETRY_EXCLUDE_EXCEPTIONS:-<none>}"
  if harbor_agent_is_claude_code; then
    echo "[INFO] realtime_hook_enabled: $HARBOR_CC_OPIK_ENABLE_HOOK | hook_source: $HARBOR_CC_HOOK_SOURCE"
  fi
  echo "[INFO] pip_index_url: ${PIP_INDEX_URL:-<default>} | pip_timeout: $HARBOR_PIP_DEFAULT_TIMEOUT | pip_retries: $HARBOR_PIP_RETRIES"
  echo "[INFO] api_base: ${HARBOR_API_BASE:-<empty>}"
  echo "[INFO] timeout_multiplier: $HARBOR_TIMEOUT_MULTIPLIER | agent_setup_timeout_multiplier: $HARBOR_AGENT_SETUP_TIMEOUT_MULTIPLIER"
  if harbor_agent_is_claude_code; then
    echo "[INFO] disallowed_tools: $HARBOR_DISALLOWED_TOOLS"
    echo "[INFO] append_system_prompt configured: yes"
  fi
  if [[ "$normalized_llm_kwargs" == *'"api_key":"="'* || "$normalized_llm_kwargs" == *'"api_key": "="'* ]]; then
    echo "[WARN] llm_kwargs is using placeholder api_key='='; this often yields all-zero scores"
  fi
  echo "[INFO] harbor cmd: $HARBOR_OPIK_BIN harbor run ..."

  if [[ "$HARBOR_DRY_RUN" == "1" ]]; then
    echo "[INFO] HARBOR_DRY_RUN=1, skip execution"
    return 0
  fi

  if harbor_agent_is_pi; then
    export PYTHONPATH="$HARBOR_PI_DIR:$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"
  else
    export PYTHONPATH="$HARBOR_CLAUDE_CODE_DIR:$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"
  fi
  "${cmd[@]}"

  echo "[INFO] completed"
  echo "[INFO] results: $out_dir"
  if harbor_trace_to_opik_enabled; then
    echo "[INFO] open traces in Opik project: $OPIK_PROJECT_NAME"
  fi
}

run_opencode_task() {
  harbor_apply_effective_wheel_source
  VERIFIER_UV_BIN_DIR_SOURCE="$(mktemp -d "${RUNTIME_DIR%/}/verifier-uv.opencode.XXXXXX" 2>/dev/null || true)"
  if [[ -n "$VERIFIER_UV_BIN_DIR_SOURCE" ]]; then
    prepare_verifier_uv_bin "$VERIFIER_UV_BIN_DIR_SOURCE" || true
  else
    echo "[WARN] failed to create verifier uv backup dir; verifier will use its normal uv install path" >&2
  fi
  configure_e2b_verifier_uv_upload
  if harbor_trace_to_opik_enabled; then
    normalize_opik_url_override
  fi

  if [[ "$HARBOR_DRY_RUN" != "1" ]] && ! harbor_runner_cli_ready; then
    harbor_validate_runner_cli
  fi
  if [[ "$HARBOR_DRY_RUN" != "1" && ! -x "$HARBOR_OPIK_PYTHON" ]]; then
    echo "[ERROR] HARBOR_OPIK_PYTHON not executable: $HARBOR_OPIK_PYTHON" >&2
    echo "[ERROR] set HARBOR_OPIK_PYTHON to the Python inside the Harbor runner environment" >&2
    exit 1
  fi

  if [[ "$HARBOR_DRY_RUN" != "1" ]] && harbor_trace_to_opik_enabled; then
    verify_opik_reachable
    verify_opik_ingestion_route
  fi

  mkdir -p "$JOBS_ROOT"
  local job_name out_dir
  job_name="$(date +%Y-%m-%d__%H-%M-%S)"
  out_dir="$JOBS_ROOT/$job_name"
  if harbor_publishes_job_dir; then
    printf '%s\n' "$out_dir" > "$HARBOR_JOB_DIR_FILE"
  fi
  mkdir -p "$out_dir"

  local effective_harbor_task_id include_task
  effective_harbor_task_id="${HARBOR_TASK_ID:-}"
  if [[ -z "$effective_harbor_task_id" ]]; then
    include_task="$(single_include_task "${INCLUDE_TASKS:-${HARBOR_INCLUDE_TASKS:-}}" || true)"
    if [[ -n "$include_task" ]]; then
      effective_harbor_task_id="$include_task"
    fi
  fi

  local opik_host no_proxy_value
  opik_host="$(
    python3 "$SCRIPT_DIR/harbor_shell_utils.py" url-hostname "$OPIK_URL_OVERRIDE"
  )"
  no_proxy_value="127.0.0.1,localhost,host.docker.internal"
  if [[ -n "$opik_host" ]]; then
    no_proxy_value="$no_proxy_value,$opik_host"
  fi

  local cmd opencode_n_concurrent
  opencode_n_concurrent="1"
  if harbor_is_native_registry_main || harbor_is_fixer_verification_main; then
    # Registry and Fixer verification runs use one Harbor process, so it must
    # own the requested concurrency. Normal local runs shard across workers.
    opencode_n_concurrent="$HARBOR_N_CONCURRENT"
  fi
  build_opencode_cmd() {
    local trial_id="$1"
    local opencode_tgz_url=""
    local opencode_linux_x64_tgz_url=""
    if [[ -n "${HARBOR_LOCAL_WHEEL_SERVER_URL:-}" ]]; then
      opencode_tgz_url="${HARBOR_LOCAL_WHEEL_SERVER_URL%/}/${OPENCODE_TGZ_BASENAME}"
      opencode_linux_x64_tgz_url="${HARBOR_LOCAL_WHEEL_SERVER_URL%/}/${OPENCODE_LINUX_X64_TGZ_BASENAME}"
    fi
    cmd=(
      "$HARBOR_OPIK_PYTHON" "$HARBOR_OPENCODE_DIR/enable_track_harbor.py" run
      -y
      --n-concurrent "$opencode_n_concurrent"
      --max-retries "$HARBOR_MAX_RETRIES"
      -o "$out_dir"
      -k 1
      --ak "version=$OPENCODE_VERSION"
      --agent-import-path opik_opencode_harbor:OpikOpenCodeHarbor
      -m "$HARBOR_MODEL"
      --ae "CC_OPIK_PY_WHEEL_DIR=$HARBOR_CC_PY_WHEEL_DIR_MOUNT_PATH"
      --ae "HARBOR_LOCAL_WHEEL_SERVER_URL=${HARBOR_LOCAL_WHEEL_SERVER_URL:-}"
      --ae "OPENCODE_TGZ_PATH=$HARBOR_CC_PY_WHEEL_DIR_MOUNT_PATH/$OPENCODE_TGZ_BASENAME"
      --ae "OPENCODE_LINUX_X64_TGZ_PATH=$HARBOR_CC_PY_WHEEL_DIR_MOUNT_PATH/$OPENCODE_LINUX_X64_TGZ_BASENAME"
      --ae "HARBOR_LOCAL_OPENCODE_TGZ_URL=$opencode_tgz_url"
      --ae "HARBOR_LOCAL_OPENCODE_LINUX_X64_TGZ_URL=$opencode_linux_x64_tgz_url"
      --ae "HARBOR_DATASET=$(harbor_metadata_dataset_name)"
      --ae "HARBOR_RUN_ID=${HARBOR_RUN_ID:-$RUN_ID}"
      --ae "HARBOR_TASK_ID=$effective_harbor_task_id"
      --ae "HARBOR_INCLUDE_TASKS=${HARBOR_INCLUDE_TASKS:-$INCLUDE_TASKS}"
      --ae "INCLUDE_TASKS=$INCLUDE_TASKS"
      --ae "HARBOR_TRIAL_ID=$trial_id"
      --ae "NO_PROXY=$no_proxy_value"
      --ae "no_proxy=$no_proxy_value"
      --timeout-multiplier "$HARBOR_TIMEOUT_MULTIPLIER"
      --agent-setup-timeout-multiplier "$HARBOR_AGENT_SETUP_TIMEOUT_MULTIPLIER"
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
    if harbor_uses_local_opensandbox_dataset; then
      cmd+=( --path "$DATASET_PATH" )
    elif harbor_uses_registry_dataset; then
      cmd+=( --dataset "$(harbor_registry_dataset_name)" )
    else
      cmd+=( --path "$DATASET_PATH" )
    fi
    prepare_opensandbox_image_ref "$out_dir/opensandbox-bundle.json" || return 1
    append_environment_backend_args

    if [[ -n "${HARBOR_AGENT_TIMEOUT_MULTIPLIER:-}" ]]; then
      cmd+=( --agent-timeout-multiplier "$HARBOR_AGENT_TIMEOUT_MULTIPLIER" )
    fi

    if [[ -n "${OPENCODE_CONFIG_CONTENT:-}" ]]; then
      cmd+=( --ak "opencode_config=$OPENCODE_CONFIG_CONTENT" )
    fi
    if [[ -z "${OPENCODE_CONFIG_CONTENT:-}" || "${HARBOR_MODEL%%/*}" != "custom" ]] \
      && [[ -n "${HARBOR_ANTHROPIC_BASE_URL:-}" ]]; then
      cmd+=( --ae "ANTHROPIC_BASE_URL=$HARBOR_ANTHROPIC_BASE_URL" )
    fi

    local mounts_json="[]"
    if [[ "$HARBOR_ENVIRONMENT_TYPE" == "docker" || "$HARBOR_ENVIRONMENT_TYPE" == "opensandbox" ]]; then
      local -a mount_args=()
      if [[ -n "$HARBOR_CC_PY_WHEEL_DIR_SOURCE" ]]; then
        mount_args+=( --mount "$HARBOR_CC_PY_WHEEL_DIR_SOURCE" "$HARBOR_CC_PY_WHEEL_DIR_MOUNT_PATH" exists )
      fi
      if [[ -n "$VERIFIER_UV_BIN_DIR_SOURCE" ]]; then
        mount_args+=( --mount "$VERIFIER_UV_BIN_DIR_SOURCE" "$HARBOR_VERIFIER_UV_BIN_DIR_MOUNT_PATH" uv-bin )
      fi
      mounts_json="$(
        python3 "$SCRIPT_DIR/harbor_shell_utils.py" readonly-mounts \
          "${mount_args[@]}"
      )"
    fi
    if [[ "$mounts_json" != "[]" ]]; then
      cmd+=( --mounts-json "$mounts_json" )
    fi
    if opensandbox_verifier_tool_env_required; then
      append_opensandbox_verifier_tool_env
    elif [[ "$HARBOR_ENVIRONMENT_TYPE" == "docker" || "$HARBOR_ENVIRONMENT_TYPE" == "e2b" \
      || "$HARBOR_ENVIRONMENT_TYPE" == "qz" ]] && verifier_uv_bin_ready; then
      cmd+=(
        --ve "PATH=/root/.local/bin:/home/oai/.local/bin:/home/agent/.local/bin:/home/ubuntu/.local/bin:$HARBOR_VERIFIER_UV_BIN_DIR_MOUNT_PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        --ve "HARBOR_VERIFIER_UV_BIN_DIR=$HARBOR_VERIFIER_UV_BIN_DIR_MOUNT_PATH"
      )
    fi

    for env_name in OC_OPIK_DEBUG OC_OPIK_DRY_RUN OC_OPIK_MAX_TEXT_CHARS OC_OPIK_FLUSH_INTERVAL_S; do
      if [[ -n "${!env_name:-}" ]]; then
        cmd+=( --ae "${env_name}=${!env_name}" )
      fi
    done

    append_package_environment_args

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

    if [[ "$HARBOR_DEBUG" == "1" ]]; then
      cmd+=( --debug )
    fi
  }

  echo "[INFO] opencode run attempts=$N_ATTEMPTS"
  echo "[INFO] project: $OPIK_PROJECT_NAME"
  echo "[INFO] output dir: $out_dir"
  if harbor_uses_local_opensandbox_dataset; then
    echo "[INFO] path: $DATASET_PATH"
  elif harbor_uses_registry_dataset; then
    echo "[INFO] dataset: $(harbor_registry_dataset_name)"
  else
    echo "[INFO] path: $DATASET_PATH"
  fi
  echo "[INFO] model: $HARBOR_MODEL"
  echo "[INFO] opencode version: $OPENCODE_VERSION"

  export PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"

  local overall_rc=0
  local attempt trial_id rc
  for ((attempt = 1; attempt <= N_ATTEMPTS; attempt++)); do
    trial_id="attempt-${attempt}"
    build_opencode_cmd "$trial_id"
    echo "[INFO] attempt $attempt/$N_ATTEMPTS trial_id=$trial_id"
    echo "[INFO] harbor cmd: $HARBOR_OPIK_PYTHON $HARBOR_OPENCODE_DIR/enable_track_harbor.py run ..."

    if [[ "$HARBOR_DRY_RUN" == "1" ]]; then
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
  harbor_validate_generation_controls
  validate_environment_backend
  configure_trace_disabled_runtime
  harbor_init_run_dirs
  if ! harbor_agent_is_oracle && harbor_trace_to_opik_enabled; then
    normalize_opik_url_override
  fi
  if [[ "$AGENT" == "oracle" ]]; then
    need_cmd python3
    need_cmd uv
    apply_min_test_defaults
    if [[ "$HARBOR_DRY_RUN" != "1" ]]; then
      ensure_environment_backend
      if ! harbor_uses_registry_dataset && [[ ! -d "$DATASET_PATH" ]]; then
        echo "[ERROR] local dataset path not found: $DATASET_PATH" >&2
        exit 1
      fi
    fi
    run_oracle_task
    return $?
  fi

  if harbor_agent_is_opencode; then
    need_cmd curl
    need_cmd python3
    ensure_trace_plugin_source_if_needed
    apply_min_test_defaults

    if [[ "$HARBOR_DRY_RUN" == "1" ]]; then
      echo "[INFO] HARBOR_DRY_RUN=1, skip dataset/opik readiness checks"
      run_opencode_task
      return $?
    fi

    ensure_environment_backend

    if ! harbor_trace_to_opik_enabled; then
      echo "[INFO] Opik tracing disabled, skip readiness checks"
    fi
    prepare_local_dataset_if_needed

    run_opencode_task
    return $?
  fi

  need_cmd git
  need_cmd curl
  need_cmd python3
  ensure_trace_plugin_source_if_needed

  if [[ -z "$AGENT" && -z "$HARBOR_AGENT_IMPORT_PATH" ]]; then
    echo "[ERROR] at least one of AGENT or HARBOR_AGENT_IMPORT_PATH must be set" >&2
    exit 1
  fi

  if [[ -z "$HARBOR_AGENT_IMPORT_PATH" && "$AGENT" != "claude-code" && "$AGENT" != "oracle" ]]; then
    echo "[ERROR] when HARBOR_AGENT_IMPORT_PATH is empty, AGENT must be claude-code or oracle (got: $AGENT)" >&2
    exit 1
  fi

  apply_min_test_defaults

  if [[ "$HARBOR_DRY_RUN" == "1" ]]; then
    echo "[INFO] HARBOR_DRY_RUN=1, skip dataset/opik readiness checks"
    if harbor_agent_is_oracle; then
      run_oracle_task
    else
      run_harbor
    fi
    return 0
  fi

  ensure_environment_backend

  if harbor_agent_is_oracle; then
    prepare_local_dataset_if_needed
    run_oracle_task
    return $?
  fi

  if ! harbor_trace_to_opik_enabled; then
    echo "[INFO] Opik tracing disabled, skip readiness checks"
  fi
  prepare_local_dataset_if_needed
  if harbor_trace_to_opik_enabled; then
    verify_opik_reachable
    verify_opik_ingestion_route
  fi
  run_harbor
}

main
