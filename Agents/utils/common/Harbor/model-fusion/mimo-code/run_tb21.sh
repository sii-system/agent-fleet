#!/usr/bin/env bash
set -euo pipefail

MIMO_CODE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARBOR_DIR="${HARBOR_DIR:-$(cd "$MIMO_CODE_DIR/../.." && pwd)}"
REPO_ROOT="$(cd "$MIMO_CODE_DIR/../../../../../.." && pwd)"

MODE="${1:-}"
TASK_ID="${2:-${TASK_ID:-configure-git-webserver}}"

usage() {
  cat <<'EOF'
Run Fusion Router's Mimo Max pipeline through Agent Fleet.

Usage:
  run_tb21.sh build
  run_tb21.sh doctor
  run_tb21.sh dry-run [task-id]
  run_tb21.sh smoke [task-id]
  run_tb21.sh full

The caller or config.local.env owns API_KEY, BASE_URL, Opik, dataset, model,
worker, and trial settings. Full mode defaults to the complete TB 2.1 task list.
EOF
}

die() {
  echo "[mimo-code] ERROR: $*" >&2
  exit 2
}

case "$MODE" in
  build|doctor|dry-run|smoke|full) ;;
  help|-h|--help|"")
    usage
    [[ -n "$MODE" ]] || exit 2
    exit 0
    ;;
  *) usage >&2; die "unknown mode: $MODE" ;;
esac

FUSION_ROUTER_DIR="${FUSION_ROUTER_DIR:-$(cd "$REPO_ROOT/.." && pwd)/sii-fusion-router}"
[[ "$(git -C "$FUSION_ROUTER_DIR" rev-parse --is-inside-work-tree 2>/dev/null || true)" == "true" ]] \
  || die "Router git checkout not found: $FUSION_ROUTER_DIR"
command -v python3 >/dev/null 2>&1 || die "python3 is required"
command -v uv >/dev/null 2>&1 || die "uv is required to build the Router wheel"

stamp="$(date +%Y%m%d-%H%M%S)"
RUN_ID="${RUN_ID:-mimo-max-${stamp}-$$}"
OPIK_PROJECT_NAME="${OPIK_PROJECT_NAME:-$RUN_ID}"
AGENT=claude-code
export RUN_ID OPIK_PROJECT_NAME AGENT

# shellcheck source=/dev/null
. "$HARBOR_DIR/env.sh"

router_version="$(
  PYTHONPATH="$FUSION_ROUTER_DIR/src" \
    python3 -c 'import sii_fusion_router; print(sii_fusion_router.__version__)'
)"
router_commit="$(git -C "$FUSION_ROUTER_DIR" rev-parse HEAD)"
router_short="${router_commit:0:12}"
wheel_metadata="$(
  python3 "$MIMO_CODE_DIR/../router_cli_utils.py" build-wheel \
    --repo "$FUSION_ROUTER_DIR" \
    --cache-root "${MIMO_ROUTER_DIST_DIR:-${OUTPUT_ROOT}/.mimo-router}" \
    --version "$router_version"
)"
mapfile -t wheel_values < <(
  python3 - "$wheel_metadata" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
for key in ("cache_dir", "source_hash", "wheel", "wheel_sha256"):
    print(payload[key])
PY
)
[[ "${#wheel_values[@]}" -eq 4 ]] || die "invalid Router wheel metadata"
dist_dir="${wheel_values[0]}"
router_source_hash="${wheel_values[1]}"
MIMO_ROUTER_WHEEL="${wheel_values[2]}"
router_wheel_sha256="${wheel_values[3]}"
router_source_short="${router_source_hash:0:12}"

runtime_dir="$(mktemp -d "${TMPDIR:-/tmp}/mimo-router-runtime.XXXXXX")"
task_file=""
cleanup() {
  rm -rf -- "$runtime_dir"
  [[ -z "$task_file" || ! -f "$task_file" ]] || rm -f -- "$task_file"
}
trap cleanup EXIT INT TERM
python3 -c 'import sys,zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' \
  "$MIMO_ROUTER_WHEEL" "$runtime_dir"
actual_version="$(PYTHONPATH="$runtime_dir" python3 -m sii_fusion_router.cli --version)"
[[ "$actual_version" == "$router_version" ]] \
  || die "wheel version $actual_version does not match source $router_version"

source_config="${MIMO_ROUTER_SOURCE_CONFIG:-$FUSION_ROUTER_DIR/examples/router-config.json}"
[[ -f "$source_config" ]] || die "Router config not found: $source_config"
MIMO_ROUTER_CONFIG="$(
  python3 "$MIMO_CODE_DIR/../router_cli_utils.py" derive-config \
    --source "$source_config" \
    --output-dir "$dist_dir" \
    --pipeline mimo_max \
    --max-fusions -1
)"

doctor_json="$(
  ANTHROPIC_BASE_URL="${BASE_URL:-http://127.0.0.1:9}" \
    PYTHONPATH="$runtime_dir" \
    python3 -m sii_fusion_router.cli doctor \
      --pipeline mimo_max \
      --config "$MIMO_ROUTER_CONFIG" \
      --claude-bin true
)"
python3 - "$doctor_json" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
if payload.get("status") != "ok" or payload.get("pipeline") != "mimo_max":
    raise SystemExit(f"Router doctor failed: {payload}")
PY

printf '[mimo-code] wheel=%s\n' "$MIMO_ROUTER_WHEEL"
printf '[mimo-code] config=%s\n' "$MIMO_ROUTER_CONFIG"
printf '[mimo-code] router=%s@%s source=%s\n' \
  "$router_version" "$router_short" "$router_source_short"
printf '[mimo-code] wheel_sha256=%s\n' "$router_wheel_sha256"
if [[ "$MODE" == "build" || "$MODE" == "doctor" ]]; then
  exit 0
fi

[[ -n "${BASE_URL:-}" ]] || die "BASE_URL is required"
[[ -n "${API_KEY:-}" && "${API_KEY:-}" != "xxx" ]] || die "API_KEY is required"
harbor_uses_registry_dataset || [[ -d "$DATASET_PATH" ]] \
  || die "DATASET_PATH not found: $DATASET_PATH"

export MIMO_CODE_DIR FUSION_ROUTER_DIR
export MIMO_ROUTER_WHEEL MIMO_ROUTER_VERSION="$router_version" MIMO_ROUTER_CONFIG
export MIMO_ROUTER_PIPELINE=mimo_max
# shellcheck source=/dev/null
. "$MIMO_CODE_DIR/env.sh"

MIMO_CODE_REAL_HARBOR_OPIK_BIN="$HARBOR_OPIK_BIN"
HARBOR_OPIK_BIN="$MIMO_CODE_DIR/harboropik.sh"
HARBOR_CLAUDE_CODE_DIR="$MIMO_CODE_DIR"
export MIMO_CODE_REAL_HARBOR_OPIK_BIN HARBOR_OPIK_BIN HARBOR_CLAUDE_CODE_DIR

export HARBOR_AGENT_TIMEOUT_MULTIPLIER="${HARBOR_AGENT_TIMEOUT_MULTIPLIER:-20}"
export HARBOR_ANTHROPIC_MODEL="${HARBOR_ANTHROPIC_MODEL:-$MODEL}"
export HARBOR_ANTHROPIC_DEFAULT_OPUS_MODEL="${HARBOR_ANTHROPIC_DEFAULT_OPUS_MODEL:-$MODEL}"
export HARBOR_ANTHROPIC_DEFAULT_SONNET_MODEL="${HARBOR_ANTHROPIC_DEFAULT_SONNET_MODEL:-$MODEL}"
export HARBOR_ANTHROPIC_DEFAULT_HAIKU_MODEL="${HARBOR_ANTHROPIC_DEFAULT_HAIKU_MODEL:-$MODEL}"
export HARBOR_CLAUDE_CODE_SUBAGENT_MODEL="${HARBOR_CLAUDE_CODE_SUBAGENT_MODEL:-$MODEL}"

if [[ "$MODE" == "dry-run" ]]; then
  export HARBOR_DRY_RUN=1 MIN_TEST=1
  export MIN_TEST_INCLUDE_TASK="$TASK_ID"
  export INCLUDE_TASKS="$TASK_ID" HARBOR_INCLUDE_TASKS="$TASK_ID"
  mkdir -p "$OUTPUT_PATH" "$RUNTIME_DIR"
  proxy_args=(harbor run)
  if harbor_uses_registry_dataset; then
    proxy_args+=(--dataset "$(harbor_registry_dataset_name)")
  else
    proxy_args+=(--path "$DATASET_PATH")
  fi
  proxy_args+=(-i "$(harbor_registry_task_name "$TASK_ID")")
  MODEL_FUSION_PROXY_RENDER_ONLY=1 "$HARBOR_OPIK_BIN" "${proxy_args[@]}"
  exit 0
fi

export HARBOR_DRY_RUN=0 MIN_TEST=0

if [[ "$MODE" == "smoke" ]]; then
  task_file="$(mktemp "${TMPDIR:-/tmp}/mimo-code-task.XXXXXX")"
  printf '%s\n' "$TASK_ID" > "$task_file"
  export TASK_SOURCE_FILE="$task_file"
  export INCLUDE_TASKS="$TASK_ID" HARBOR_INCLUDE_TASKS="$TASK_ID"
  export TOTAL_WORKERS=1 HARBOR_N_CONCURRENT=1
  export N_ATTEMPTS=1 HARBOR_RUNS=1 MAX_RETRIES=0 HARBOR_MAX_RETRIES=0
else
  TASK_SOURCE_FILE="${TASK_SOURCE_FILE:-$REPO_ROOT/Tasks/Terminal-bench-2/harbor_terminalbench21_tasks.txt}"
  [[ -s "$TASK_SOURCE_FILE" ]] || die "task list not found or empty: $TASK_SOURCE_FILE"
  INCLUDE_TASKS="$(python3 - "$TASK_SOURCE_FILE" <<'PY'
import sys
from pathlib import Path

tasks = [line.strip() for line in Path(sys.argv[1]).read_text().splitlines()]
tasks = [task for task in tasks if task and not task.startswith("#")]
if not tasks or any("," in task for task in tasks):
    raise SystemExit("task list must contain nonempty task IDs without commas")
print(",".join(tasks))
PY
)"
  export TASK_SOURCE_FILE INCLUDE_TASKS HARBOR_INCLUDE_TASKS="$INCLUDE_TASKS"
  export HARBOR_RUNS="${HARBOR_RUNS:-${N_ATTEMPTS:-1}}"
  export HARBOR_MAX_RETRIES="${HARBOR_MAX_RETRIES:-${MAX_RETRIES:-0}}"
fi

start_args=()
[[ "${DETACH:-1}" != "1" ]] || start_args+=(--detach)
(cd "$HARBOR_DIR" && bash start.sh "${start_args[@]}")

printf '[mimo-code] run_id=%s\n' "$RUN_ID"
printf '[mimo-code] output_path=%s\n' "$OUTPUT_PATH"
