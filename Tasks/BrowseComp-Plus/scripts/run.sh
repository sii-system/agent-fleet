#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCHMARK_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$BENCHMARK_DIR/../.." && pwd)"
# shellcheck source=../../../scripts/prerequisites.sh
source "$REPO_ROOT/scripts/prerequisites.sh"
agent_fleet_prerequisite_init_path
# shellcheck source=../../../scripts/config_loader.sh
source "$REPO_ROOT/scripts/config_loader.sh"
agent_fleet_load_config "$REPO_ROOT"

if [[ -n "${BROWSECOMP_CONFIG:-}" ]]; then
  [[ -f "$BROWSECOMP_CONFIG" ]] || { echo "[ERROR] BROWSECOMP_CONFIG not found: $BROWSECOMP_CONFIG" >&2; exit 1; }
  set -a
  # shellcheck disable=SC1090
  source "$BROWSECOMP_CONFIG"
  set +a
fi

usage() {
  cat <<EOF
Usage: $0 [--task id[,id...]] [--agent claude-code|opencode|pi] [--workers N]
          [--detach] [--dry-run] [--validate-tasks-only]
          [--prepare-only|--collect-only|--evaluate-only]

The default Qwen3-Embedding-0.6B runtime, data, and index are prepared and
cached automatically. Optional BROWSECOMP_* overrides can live in
config.local.env; see $BENCHMARK_DIR/config/browsecomp.env.example.
EOF
}

TASKS="${FLEET_TASKS:-}"
AGENT_NAME="${AGENT:-claude-code}"
WORKERS="${TOTAL_WORKERS:-1}"
DETACH=0
DRY_RUN=0
VALIDATE_ONLY=0
MODE=run
while [[ $# -gt 0 ]]; do
  case "$1" in
    --task) TASKS="${2:?--task requires a value}"; shift 2 ;;
    --agent) AGENT_NAME="${2:?--agent requires a value}"; shift 2 ;;
    --workers) WORKERS="${2:?--workers requires a value}"; shift 2 ;;
    --detach) DETACH=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --validate-tasks-only) VALIDATE_ONLY=1; shift ;;
    --download) shift ;; # compatibility: downloads are automatic now
    --prepare-only) MODE=prepare; shift ;;
    --collect-only) MODE=collect; shift ;;
    --evaluate-only) MODE=evaluate; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "[ERROR] unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$AGENT_NAME" in claude-code|opencode|pi) ;; *) echo "[ERROR] unsupported BrowseComp harness: $AGENT_NAME" >&2; exit 2 ;; esac
[[ "$WORKERS" =~ ^[1-9][0-9]*$ ]] || { echo "[ERROR] --workers must be a positive integer" >&2; exit 2; }

RUN_ID="${RUN_ID:-$(date +%Y-%m-%d-%H%M%S)-browsecomp-plus}"
OUTPUT_PATH="${OUTPUT_PATH:-$REPO_ROOT/runs/$RUN_ID}"
BCP_RUN_DIR="$OUTPUT_PATH/browsecomp-plus"
SOURCE_ROOT="${BROWSECOMP_SOURCE_ROOT:-$REPO_ROOT/third_party/BrowseComp-Plus}"
CACHE_ROOT="${BROWSECOMP_CACHE_ROOT:-${AGENT_FLEET_CACHE_DIR:-$HOME/.cache/agent-fleet}/browsecomp-plus}"
[[ "$SOURCE_ROOT" == /* ]] || SOURCE_ROOT="$REPO_ROOT/$SOURCE_ROOT"
[[ "$CACHE_ROOT" == /* ]] || CACHE_ROOT="$REPO_ROOT/$CACHE_ROOT"

INDEX_VARIANT="${BROWSECOMP_INDEX_VARIANT:-qwen3-embedding-0.6b}"
GROUND_TRUTH="${BROWSECOMP_GROUND_TRUTH:-$CACHE_ROOT/private/browsecomp_plus_decrypted.jsonl}"
INDEX_PATH="${BROWSECOMP_INDEX_PATH:-$CACHE_ROOT/indexes/$INDEX_VARIANT/corpus.shard*.pkl}"
[[ "$GROUND_TRUTH" == /* ]] || GROUND_TRUTH="$REPO_ROOT/$GROUND_TRUTH"
[[ "$INDEX_PATH" == /* ]] || INDEX_PATH="$REPO_ROOT/$INDEX_PATH"
export BROWSECOMP_SOURCE_ROOT="$SOURCE_ROOT"
export BROWSECOMP_CACHE_ROOT="$CACHE_ROOT"
export BROWSECOMP_GROUND_TRUTH="$GROUND_TRUTH"
export BROWSECOMP_INDEX_PATH="$INDEX_PATH"
export BROWSECOMP_PYTHON="${BROWSECOMP_PYTHON:-$CACHE_ROOT/runtime/venv/bin/python}"
export BROWSECOMP_JUDGE_PYTHON="${BROWSECOMP_JUDGE_PYTHON:-$BROWSECOMP_PYTHON}"
export HF_HOME="${HF_HOME:-$CACHE_ROOT/huggingface}"
export HF_DATASETS_CACHE="${HF_DATASETS_CACHE:-$HF_HOME/datasets}"
if [[ "${BROWSECOMP_OFFLINE:-0}" == "1" ]]; then
  export HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1 TRANSFORMERS_OFFLINE=1
fi

DATASET_ROOT="$BCP_RUN_DIR/tasks"
TASK_FILE="$BCP_RUN_DIR/tasks.txt"
RUN_MANIFEST="$BCP_RUN_DIR/task-manifest.json"
OFFICIAL_RUN_DIR="$BCP_RUN_DIR/official-runs/$AGENT_NAME"
EVAL_DIR="$BCP_RUN_DIR/evals/$AGENT_NAME"
AGENT_ENV_FILE="$BCP_RUN_DIR/runtime/agent.env"
MCP_CONFIG="$BCP_RUN_DIR/runtime/mcp.json"
PI_EXTENSION_BUNDLE="$BCP_RUN_DIR/runtime/pi-extensions"
HARBOR_JOBS_ROOT="$OUTPUT_PATH/jobs/$AGENT_NAME"
if [[ -z "${BROWSECOMP_MCP_PORT:-}" ]]; then
  BROWSECOMP_MCP_PORT="$(
    python3 "$BENCHMARK_DIR/mcp/launcher.py" resolve-port \
      --source-root "$SOURCE_ROOT" \
      --state-dir "$CACHE_ROOT/mcp"
  )"
  export BROWSECOMP_MCP_PORT
fi

# Harbor's Docker allowlist is implemented by putting the task container in an
# egress sidecar's network namespace. Docker rejects extra_hosts together with
# that network mode, so reach host services through the bridge gateway IP
# instead of relying on a host.docker.internal host-gateway mapping.
resolve_mcp_host_ip() {
  local resolved="${BROWSECOMP_MCP_HOST_IP:-${LOCAL_WHEEL_HOST_IP:-}}"
  if [[ -z "$resolved" ]] && command -v ip >/dev/null 2>&1; then
    resolved="$(ip -4 addr show docker0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1 | head -n 1 || true)"
  fi
  if [[ -z "$resolved" ]] && command -v docker >/dev/null 2>&1; then
    resolved="$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)"
  fi
  if [[ -z "$resolved" ]]; then
    echo "[ERROR] could not determine the Docker host gateway for the BrowseComp MCP service" >&2
    echo "[ERROR] set BROWSECOMP_MCP_HOST_IP or BROWSECOMP_MCP_PUBLIC_URL explicitly" >&2
    return 1
  fi
  printf '%s\n' "$resolved"
}

if [[ -z "${BROWSECOMP_MCP_PUBLIC_URL:-}" ]]; then
  BROWSECOMP_MCP_HOST_IP="$(resolve_mcp_host_ip)"
  BROWSECOMP_MCP_PUBLIC_URL="http://${BROWSECOMP_MCP_HOST_IP}:${BROWSECOMP_MCP_PORT}/mcp"
  export BROWSECOMP_MCP_HOST_IP BROWSECOMP_MCP_PUBLIC_URL
fi
MCP_URL="$BROWSECOMP_MCP_PUBLIC_URL"
MCP_ALLOWED_HOST="$(python3 "$REPO_ROOT/scripts/script_utils.py" url-hostname "$MCP_URL")"
[[ -n "$MCP_ALLOWED_HOST" ]] || { echo "[ERROR] invalid BROWSECOMP_MCP_PUBLIC_URL: $MCP_URL" >&2; exit 2; }
BROWSECOMP_ALLOWED_HOSTS=()
append_allowed_host() {
  local allowed_host="$1"
  [[ -n "$allowed_host" ]] || return 0
  local existing_host
  for existing_host in "${BROWSECOMP_ALLOWED_HOSTS[@]:-}"; do
    [[ "$existing_host" != "$allowed_host" ]] || return 0
  done
  BROWSECOMP_ALLOWED_HOSTS+=("$allowed_host")
}
for endpoint in "${HARBOR_ANTHROPIC_BASE_URL:-${BASE_URL:-}}" "$MCP_URL"; do
  [[ -n "$endpoint" ]] || continue
  append_allowed_host "$(python3 "$REPO_ROOT/scripts/script_utils.py" url-hostname "$endpoint")"
done
BCP_ENVIRONMENT_TYPE="${BROWSECOMP_ENVIRONMENT_TYPE:-docker}"
if [[ "$BCP_ENVIRONMENT_TYPE" != docker ]]; then
  echo "[ERROR] self-contained BrowseComp currently requires BROWSECOMP_ENVIRONMENT_TYPE=docker" >&2
  echo "[ERROR] remote sandboxes cannot reach the local MCP retriever" >&2
  exit 2
fi

# Harbor serves its managed runner dependencies from the Docker host.  Keep
# those health checks (and the benchmark MCP endpoint) away from an inherited
# corporate HTTP proxy, which can otherwise make a dead local port look ready.
HARBOR_NO_PROXY="${NO_PROXY:-${no_proxy:-}}"
append_no_proxy_host() {
  local local_host="$1"
  [[ -n "$local_host" ]] || return 0
  case ",$HARBOR_NO_PROXY," in
    *",$local_host,"*) ;;
    *) HARBOR_NO_PROXY="${HARBOR_NO_PROXY:+$HARBOR_NO_PROXY,}$local_host" ;;
  esac
}
for local_host in 127.0.0.1 localhost ::1 "${LOCAL_WHEEL_HOST_IP:-}" "${BROWSECOMP_MCP_HOST_IP:-}" "$MCP_ALLOWED_HOST"; do
  append_no_proxy_host "$local_host"
done
# Harbor sends Pi requests directly to the configured Fleet gateway. Apply the
# same policy to the optional host-side judge so reusing BASE_URL behaves the
# same in both phases.
for endpoint in "${BASE_URL:-}" "${BROWSECOMP_JUDGE_BASE_URL:-}"; do
  [[ -n "$endpoint" ]] || continue
  append_no_proxy_host "$(python3 "$REPO_ROOT/scripts/script_utils.py" url-hostname "$endpoint")"
done
if [[ "${BROWSECOMP_EMBEDDING_BACKEND:-local}" == "openai" ]]; then
  case "${BROWSECOMP_EMBEDDING_PROXY_MODE:-direct}" in
    direct)
      append_no_proxy_host "$(python3 "$REPO_ROOT/scripts/script_utils.py" url-hostname "${BROWSECOMP_EMBEDDING_BASE_URL:-}")"
      ;;
    inherit) ;;
    *) echo "[ERROR] BROWSECOMP_EMBEDDING_PROXY_MODE must be direct or inherit" >&2; exit 2 ;;
  esac
fi
export NO_PROXY="$HARBOR_NO_PROXY"
export no_proxy="$HARBOR_NO_PROXY"

bootstrap_cmd=(python3 "$SCRIPT_DIR/bootstrap.py" --source-root "$SOURCE_ROOT" --cache-root "$CACHE_ROOT" --ground-truth "$GROUND_TRUTH" --index-path "$INDEX_PATH" --index-variant "$INDEX_VARIANT")
[[ "${BROWSECOMP_JUDGE_MODE:-none}" != local ]] || bootstrap_cmd+=(--with-local-judge)
[[ "${BROWSECOMP_OFFLINE:-0}" != 1 ]] || bootstrap_cmd+=(--offline)
prepare_cmd=(python3 "$SCRIPT_DIR/prepare.py" --source-root "$SOURCE_ROOT" --cache-root "$CACHE_ROOT" --ground-truth "$GROUND_TRUTH" --index-path "$INDEX_PATH")
materialize_cmd=(python3 "$SCRIPT_DIR/materialize_tasks.py" --ground-truth "$GROUND_TRUTH" --output-root "$DATASET_ROOT" --task-file "$TASK_FILE" --manifest "$RUN_MANIFEST")
for allowed_host in "${BROWSECOMP_ALLOWED_HOSTS[@]}"; do
  materialize_cmd+=(--allowed-host "$allowed_host")
done
[[ -z "$TASKS" ]] || materialize_cmd+=(--tasks "$TASKS")
if [[ -z "$TASKS" && "${MIN_TEST:-0}" =~ ^[1-9][0-9]*$ ]]; then
  materialize_cmd+=(--limit "$MIN_TEST")
fi
if [[ "${RESET_RUN:-0}" != 1 && "$VALIDATE_ONLY" != 1 ]]; then
  materialize_cmd+=(--existing-task-file "$OUTPUT_PATH/tasks.txt")
fi
collect_cmd=(python3 "$SCRIPT_DIR/collect_results.py" --jobs-root "$HARBOR_JOBS_ROOT" --output-dir "$OFFICIAL_RUN_DIR" --task-manifest "$RUN_MANIFEST")
evaluate_cmd=(python3 "$SCRIPT_DIR/evaluate.py" --source-root "$SOURCE_ROOT" --ground-truth "$GROUND_TRUTH" --input-dir "$OFFICIAL_RUN_DIR" --eval-dir "$EVAL_DIR")

if (( DRY_RUN )); then
  printf 'BrowseComp-Plus plan: run_id=%s agent=%s workers=%s tasks=%s\n' "$RUN_ID" "$AGENT_NAME" "$WORKERS" "${TASKS:-${MIN_TEST:+first $MIN_TEST}}"
  printf '  vendored_source=%s\n  managed_cache=%s\n  output=%s\n  mcp=%s\n' "$SOURCE_ROOT" "$CACHE_ROOT" "$BCP_RUN_DIR" "$MCP_URL"
  printf 'Command:'; printf ' %q' "${bootstrap_cmd[@]}"; printf '\n'
  printf 'Command:'; printf ' %q' "${materialize_cmd[@]}"; printf '\n'
  printf 'Command: env NO_PROXY=%q no_proxy=%q DATASET_NAME=auto DATASET_PATH=%q AGENT=%q TOTAL_WORKERS=%q HARBOR_ENVIRONMENT_TYPE=%q bash %q%s\n' "$HARBOR_NO_PROXY" "$HARBOR_NO_PROXY" "$DATASET_ROOT" "$AGENT_NAME" "$WORKERS" "$BCP_ENVIRONMENT_TYPE" "$REPO_ROOT/Agents/utils/common/Harbor/start.sh" "$([[ $DETACH == 1 ]] && printf ' --detach')"
  exit 0
fi

mkdir -p "$BCP_RUN_DIR"
if [[ "$MODE" == collect ]]; then
  "${collect_cmd[@]}"
  exit
fi

if [[ "${BROWSECOMP_SKIP_BOOTSTRAP:-0}" != 1 ]]; then
  if [[ "$MODE" == evaluate || "$VALIDATE_ONLY" == 1 ]]; then
    "${bootstrap_cmd[@]}" --data-only
  else
    "${bootstrap_cmd[@]}"
  fi
fi

if [[ -f "$CACHE_ROOT/bootstrap.json" ]]; then
  BROWSECOMP_HF_PROXY_MODE_RESOLVED="$(
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("hf_proxy_mode", "inherit"))' \
      "$CACHE_ROOT/bootstrap.json"
  )"
  export BROWSECOMP_HF_PROXY_MODE_RESOLVED
fi

if [[ "$MODE" == evaluate ]]; then
  "${evaluate_cmd[@]}"
  exit
fi

if (( VALIDATE_ONLY )); then
  "${materialize_cmd[@]}"
  echo "Validated BrowseComp task selection: ${TASKS:-all}"
  exit 0
fi

"${prepare_cmd[@]}"
"${materialize_cmd[@]}"
HARBOR_TASK_SELECTION="$(
  python3 -c 'from pathlib import Path; import sys; print(",".join(line.strip() for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines() if line.strip()))' \
    "$TASK_FILE"
)"
[[ -n "$HARBOR_TASK_SELECTION" ]] || { echo "[ERROR] materialized BrowseComp task selection is empty" >&2; exit 1; }
if [[ "${BROWSECOMP_SKIP_MCP_START:-0}" != 1 ]]; then
  python3 "$BENCHMARK_DIR/mcp/launcher.py" start \
    --source-root "$SOURCE_ROOT" \
    --state-dir "$CACHE_ROOT/mcp" \
    --wait-seconds "${BROWSECOMP_MCP_WAIT_SECONDS:-1800}"
fi
python3 "$SCRIPT_DIR/configure_run.py" \
  --run-dir "$BCP_RUN_DIR" \
  --run-id "$RUN_ID" \
  --mcp-url "$MCP_URL" \
  --dataset-root "$DATASET_ROOT" \
  --pi-extension-source "${PI_EXTENSION_SOURCE:-$REPO_ROOT/Agents/Harbor-pi/extensions}"
if [[ "$MODE" == prepare ]]; then
  echo "BrowseComp rollout inputs ready: $BCP_RUN_DIR"
  exit 0
fi

harbor_cmd=(
  env
  "RUN_ID=$RUN_ID"
  "OUTPUT_PATH=$OUTPUT_PATH"
  "DATASET_NAME=auto"
  "DATASET_PATH=$DATASET_ROOT"
  "TASK_SOURCE_FILE=$TASK_FILE"
  "FLEET_TASKS=$HARBOR_TASK_SELECTION"
  "AGENT=$AGENT_NAME"
  "TOTAL_WORKERS=$WORKERS"
  "HARBOR_N_CONCURRENT=$WORKERS"
  "HARBOR_BENCHMARK_NAME=browsecomp-plus"
  "HARBOR_ENVIRONMENT_TYPE=$BCP_ENVIRONMENT_TYPE"
  "HARBOR_AGENT_ENV_FILE=$AGENT_ENV_FILE"
  "HARBOR_MCP_CONFIG=$MCP_CONFIG"
  "PI_EXTENSION_SOURCE=$PI_EXTENSION_BUNDLE"
  "NO_PROXY=$HARBOR_NO_PROXY"
  "no_proxy=$HARBOR_NO_PROXY"
  bash "$REPO_ROOT/Agents/utils/common/Harbor/start.sh"
)
(( DETACH == 0 )) || harbor_cmd+=(--detach)
HARBOR_LAUNCHED_AT_NS="$(python3 -c 'import time; print(time.time_ns())')"
"${harbor_cmd[@]}"

if (( DETACH )); then
  FINALIZER_LOG="$BCP_RUN_DIR/runtime/finalizer.log"
  FINALIZER_PID_FILE="$BCP_RUN_DIR/runtime/finalizer.pid"
  FINALIZER_STATUS_FILE="$BCP_RUN_DIR/runtime/finalizer.json"
  finalizer_cmd=(
    python3 "$SCRIPT_DIR/finalize_detached.py"
    --summary-file "$OUTPUT_PATH/summary.txt"
    --summary-not-before-ns "$HARBOR_LAUNCHED_AT_NS"
    --jobs-root "$HARBOR_JOBS_ROOT"
    --official-run-dir "$OFFICIAL_RUN_DIR"
    --task-manifest "$RUN_MANIFEST"
    --eval-dir "$EVAL_DIR"
    --source-root "$SOURCE_ROOT"
    --ground-truth "$GROUND_TRUTH"
    --status-file "$FINALIZER_STATUS_FILE"
    --timeout-seconds "${BROWSECOMP_DETACHED_TIMEOUT_SECONDS:-172800}"
  )
  existing_finalizer="$(cat "$FINALIZER_PID_FILE" 2>/dev/null || true)"
  if [[ ! "$existing_finalizer" =~ ^[0-9]+$ ]] || ! kill -0 "$existing_finalizer" 2>/dev/null; then
    nohup setsid "${finalizer_cmd[@]}" >>"$FINALIZER_LOG" 2>&1 </dev/null &
    existing_finalizer="$!"
    printf '%s\n' "$existing_finalizer" >"$FINALIZER_PID_FILE"
  fi
  echo "BrowseComp Harbor run detached; collection and judging will finish automatically."
  echo "Finalizer status: $FINALIZER_STATUS_FILE (pid $existing_finalizer)"
  exit 0
fi

"${collect_cmd[@]}"
"${evaluate_cmd[@]}"
